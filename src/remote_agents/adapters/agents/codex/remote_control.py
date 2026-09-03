"""Codex's host-level Remote Control, spoken as a closed table of `codex` argument vectors.

This is the one module in the project that runs a `codex` command whose effect reaches past a
pane: turning Remote Control on enrols this machine with OpenAI's relay, so the phone can
drive it. Everything about the shape here follows from that.

**The table is closed and it has one home.** Every vector is a module constant beginning with
`codex`; nothing is built from a caller's string, and the destructive teardown verb is absent
by construction rather than by discipline -- a test asserts no entry carries it. Turning
Remote Control *off* is `disable-remote-control`, which flips the persisted preference and the
running daemon in one step and leaves the daemon up. Tearing the daemon down instead would
disconnect every TUI attached to it, and those panes exit on disconnect: "off" would silently
kill the owner's running agents.

**Both collaborators are injected.** The subprocess runner and the JSON-RPC round trip are
constructor arguments, so nothing below `tests/live` spawns a real `codex`. The default
implementations are at the bottom of this module.

**Nothing this module reads is echoed.** `codex` writes paths, auth hints and prompts to
stderr; a status line built out of that text renders whatever the provider happened to say
into a Telegram message. So a failure becomes `ERRORED` -- the reading that means "the daemon
answered and would not say" -- and never a string. `serverName` is the one provider value
that is rendered, and it passes the presentation-boundary encoder first (DEC-014).

**Reading is not enabling.** `status()` only ever asks; the one place a read touches a
subprocess is the `daemon version` probe that tells "no daemon is running" apart from "the
daemon is up and failing", and that command starts nothing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol

from remote_agents.adapters.agents.protocols import JsonRpcProcess, ProtocolError
from remote_agents.domain.remote_control import (
    HostConnection,
    HostRemoteControlStatus,
    PairingCode,
    RemoteControlState,
)
from remote_agents.ports.terminal_text import encodable_text, sanitize_terminal_text

#: Every `codex` invocation this project will ever make about Remote Control, by name.
#:
#: Read-only at runtime as well as closed at authoring time: the tmux codecs get their
#: immutability free by being tuples, and a plain dict here would have made "absent by
#: construction" a claim about the author rather than about the object. One assignment
#: elsewhere in the interpreter could otherwise point `disable` at the teardown verb.
#:
#: A closed table rather than a builder, for the reason the tmux adapter's codecs are closed:
#: a vector assembled from a caller's value is a vector a caller can steer. `status` is the
#: proxy the JSON-RPC client speaks over; `daemon_probe` answers whether anything is
#: listening at all. The table carries no teardown verb and a test proves it -- see the
#: module docstring for why that matters more than it looks.
REMOTE_CONTROL_ARGV: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "status": ("codex", "app-server", "proxy"),
        "daemon_probe": ("codex", "app-server", "daemon", "version"),
        "enable": ("codex", "remote-control", "start", "--json"),
        "disable": ("codex", "app-server", "daemon", "disable-remote-control"),
        "pair": ("codex", "remote-control", "pair", "--json"),
    }
)

#: The JSON-RPC method the running daemon answers with its enrollment state (app-server v2).
STATUS_METHOD = "remoteControl/status/read"

#: What the daemon calls each state, mapped to the connection vocabulary the domain derives
#: from. Closed: a value not listed here is a protocol this adapter does not speak, and it
#: raises rather than guessing at a neighbouring meaning.
_CONNECTION_FOR_REPORTED_STATUS: dict[str, HostConnection] = {
    "connected": HostConnection.CONNECTED,
    "connecting": HostConnection.CONNECTING,
    "disabled": HostConnection.DISABLED,
    "errored": HostConnection.ERRORED,
}

#: The connect failure a missing daemon answers with names its control socket. Matching on
#: the socket's filename rather than the whole path, which carries the operator's home.
#:
#: This is a coupling to *unversioned CLI stderr wording*, not to a documented contract
#: (DEC-063: Codex facts are convention). It fails closed if the wording moves -- the reading
#: becomes a raised `ProtocolError` rather than a wrong "off" on a host where it is on -- which
#: is why a substring match is tolerable here and would not be if the branches were reversed.
_ABSENT_DAEMON_SIGNATURE = "app-server-control.sock"

# Enabling waits for the enrollment websocket, which is a network round trip to the relay;
# the other two are local. Each is a ceiling, not an expectation -- the point is that a
# `codex` command which never returns cannot hold the operation lock forever.
_ENABLE_TIMEOUT_SECONDS = 30.0
_DISABLE_TIMEOUT_SECONDS = 15.0
_PAIR_TIMEOUT_SECONDS = 15.0
_PROBE_TIMEOUT_SECONDS = 10.0

#: A server name is a machine name. Bounded because it is rendered into a Telegram message
#: and a TUI line, and this project did not decode it.
_SERVER_NAME_MAX_BYTES = 256


@dataclass(frozen=True, slots=True)
class CommandResult:
    """One finished `codex` invocation, as its runner observed it."""

    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Argument-vector subprocess boundary, bounded by an explicit timeout."""

    async def run(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult: ...


class DaemonRpc(Protocol):
    """One JSON-RPC round trip to the running daemon, over `codex app-server proxy`."""

    async def request(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]: ...


class CodexRemoteControl:
    """Codex's `HostRemoteControl`: read the daemon, flip it, mint a pairing code."""

    def __init__(self, runner: CommandRunner | None = None, rpc: DaemonRpc | None = None) -> None:
        self._runner: CommandRunner = runner or AsyncCommandRunner()
        self._rpc: DaemonRpc = rpc or AppServerProxyRpc()

    async def status(self) -> HostRemoteControlStatus:
        """Ask the daemon what it is doing, starting nothing and changing nothing."""
        try:
            payload = await self._rpc.request(STATUS_METHOD, {})
        except ProtocolError:
            if await self._no_daemon_is_listening():
                return HostRemoteControlStatus.observed(
                    HostConnection.DAEMON_ABSENT, server_name=None
                )
            raise
        return self._reading(payload)

    async def set_state(self, desired: RemoteControlState) -> HostRemoteControlStatus:
        """Drive the host to `desired` and answer with the reading that followed."""
        if desired is RemoteControlState.ACTIVE:
            return await self._enable()
        if desired is RemoteControlState.INACTIVE:
            return await self._disable()
        raise ValueError(
            f"{desired} is what a reading says, not a state a caller can ask for -- "
            "there is no command that makes the daemon uncertain on purpose"
        )

    async def aclose(self) -> None:
        """Close the proxy session, if one was opened and the client can close it.

        `status()` opens a long-lived `codex app-server proxy` child on first use. Without
        this, a caller constructing an adapter per request leaks one subprocess and its three
        pipes per construction, and nothing in the process could ever reclaim them. Tolerant
        of an injected client that has no `close` -- a test double should not have to grow a
        lifecycle to be usable.
        """
        close = getattr(self._rpc, "close", None)
        if close is not None:
            await close()

    async def pair(self) -> PairingCode:
        """Mint a short-lived manual pairing code, for rendering once and never storing."""
        result = await self._runner.run(REMOTE_CONTROL_ARGV["pair"], timeout=_PAIR_TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise ProtocolError("codex refused to mint a pairing code")
        payload = self._payload(result.stdout)
        if payload is None:
            raise ProtocolError("codex printed a pairing response this adapter cannot read")
        code = payload.get("manual_pairing_code") or payload.get("manualPairingCode")
        expires_at = payload.get("expires_at", payload.get("expiresAt"))
        # The manual code is the one a human types into the app's manual-pairing screen; the
        # short `pairing_code` is the app-to-app handshake and pairs nothing when read aloud.
        # Both spellings are accepted because Codex's JSON is convention, not contract
        # (DEC-063), and a rename would otherwise read as "pairing is broken".
        if not isinstance(code, str) or not code:
            raise ProtocolError("codex printed no manual pairing code")
        if not isinstance(expires_at, int | float):
            raise ProtocolError("codex printed a pairing code with no expiry")
        return PairingCode(code=code, expires_at=datetime.fromtimestamp(expires_at, tz=UTC))

    # ------------------------------------------------------------------ internals

    async def _enable(self) -> HostRemoteControlStatus:
        result = await self._runner.run(
            REMOTE_CONTROL_ARGV["enable"], timeout=_ENABLE_TIMEOUT_SECONDS
        )
        if result.returncode != 0:
            # A `disabled` or `errored` outcome arrives as a non-zero exit rather than a JSON
            # row, so this branch is the ordinary failure and not only the exotic one.
            return HostRemoteControlStatus.observed(HostConnection.ERRORED, server_name=None)
        payload = self._payload(result.stdout)
        if payload is None:
            return HostRemoteControlStatus.observed(HostConnection.ERRORED, server_name=None)
        try:
            return self._reading(payload)
        except ProtocolError:
            return HostRemoteControlStatus.observed(HostConnection.ERRORED, server_name=None)

    async def _disable(self) -> HostRemoteControlStatus:
        result = await self._runner.run(
            REMOTE_CONTROL_ARGV["disable"], timeout=_DISABLE_TIMEOUT_SECONDS
        )
        if result.returncode != 0:
            return HostRemoteControlStatus.observed(HostConnection.ERRORED, server_name=None)
        # Re-read rather than assert: the command prints human-readable text only, so the
        # daemon itself is the only honest source for what the flip actually achieved.
        return await self.status()

    async def _no_daemon_is_listening(self) -> bool:
        """Tell "nothing is running" apart from "the daemon is up and failing"."""
        result = await self._runner.run(
            REMOTE_CONTROL_ARGV["daemon_probe"], timeout=_PROBE_TIMEOUT_SECONDS
        )
        return result.returncode != 0 and _ABSENT_DAEMON_SIGNATURE in result.stderr

    def _reading(self, payload: Mapping[str, object]) -> HostRemoteControlStatus:
        reported = payload.get("status")
        connection = (
            _CONNECTION_FOR_REPORTED_STATUS.get(reported) if isinstance(reported, str) else None
        )
        if connection is None:
            raise ProtocolError("codex reported a remote-control state this adapter does not speak")
        return HostRemoteControlStatus.observed(
            connection, server_name=self._server_name(payload.get("serverName"))
        )

    @staticmethod
    def _payload(stdout: str) -> Mapping[str, object] | None:
        """The one JSON object `codex --json` printed, or None -- never the text itself."""
        try:
            parsed = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _server_name(raw: object) -> str | None:
        """Make the daemon's name for this machine renderable, or say there isn't one.

        Two encoders, because the value arrived through `json.loads`: `encodable_text` for a
        lone surrogate no encoder will carry, then the terminal sanitizer for the control
        characters that would otherwise reach a terminal verbatim (DEC-014, and the vault's
        `Strip Control Chars to Block Terminal Injection`).
        """
        if not isinstance(raw, str) or not raw:
            return None
        cleaned = sanitize_terminal_text(
            encodable_text(raw).encode("utf-8"), max_lines=1, max_bytes=_SERVER_NAME_MAX_BYTES
        )
        return cleaned or None


class AsyncCommandRunner:
    """Run one prevalidated `codex` vector without a shell, bounded by a timeout."""

    async def run(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ProtocolError("codex did not answer in time") from None
        return CommandResult(
            returncode=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


class AppServerProxyRpc:
    """One JSON-RPC session over `codex app-server proxy`, opened on first use."""

    def __init__(self) -> None:
        self._process = JsonRpcProcess(REMOTE_CONTROL_ARGV["status"])

    async def request(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        return await self._process.request(method, params)

    async def close(self) -> None:
        await self._process.close()
