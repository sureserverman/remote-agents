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
#: immutability free by being tuples, and a plain dict here would have let any code in the
#: interpreter repoint `disable` at a teardown verb by assignment, which every call site
#: would then pick up because they all look the table up by name at call time.
#:
#: Stated precisely, because the proxy is weaker than it first appears: it blocks assignment,
#: and it does NOT block `gc.get_referents()` reaching the underlying dict, nor rebinding this
#: module attribute wholesale. Both need arbitrary in-process code, at which point the game is
#: already lost -- so the proxy is a guard against mistakes, not against an attacker, and the
#: exact-match pin in the tests is what actually holds the table's contents.
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
        "enable_when_absent": ("codex", "remote-control", "start", "--json"),
        "enable_when_running": ("codex", "app-server", "daemon", "enable-remote-control"),
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

#: ...and the cause line that distinguishes "there is no socket" from "there is a socket I
#: cannot reach". Both failures name the socket path, so the path alone is not evidence: a
#: daemon owned by another uid (EACCES), a divergent `CODEX_HOME` between the interactive
#: shell and the user service, or a backlog refusal would all have been read as "no daemon"
#: and rendered as **off on a host that is on** -- the one direction of wrongness that
#: matters here. Requiring the ENOENT cause makes the ambiguous failures raise instead.
_ABSENT_DAEMON_CAUSES = ("No such file or directory", "os error 2")

#: What `codex` says when the *installation* cannot start a daemon.
#:
#: Only the verbs that bring a daemon up -- `remote-control start` and `app-server daemon
#: start` -- need OpenAI's standalone install at a fixed path, because that is where the
#: daemon starts and updates app-server from. The preference verbs
#: (`enable-remote-control` / `disable-remote-control`) work on either distribution.
#:
#: An earlier version of this comment, and the documentation written alongside it, claimed the
#: whole daemon surface refused. Measured afterwards on this host: `disable-remote-control`
#: exits 0 with JSON on the npm build. The check is applied to every command anyway -- it
#: costs one substring test, and the classification belongs to the message rather than to the
#: branch it was first seen in.
#:
#: Mapped to UNREACHABLE rather than ERRORED, which is where it landed first. ERRORED means
#: the daemon answered and reported its own link broken; here there is no daemon and cannot be
#: one, so an owner was shown "link broken" for a machine with no link to break and no way to
#: learn why the button did nothing -- the same "button that could never explain itself" this
#: feature exists to stop. Found by running the live drill on this host, which is what a live
#: drill is for.
_UNSUPPORTED_INSTALL_SIGNATURE = "managed standalone Codex install not found"

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

#: A manual pairing code is something a person reads off one screen and types into
#: another, so it is short by construction. Generous against the real `XXXX-XXXX` shape
#: without admitting a payload.
_PAIRING_CODE_MAX_CHARACTERS = 64


def _cannot_start_a_daemon(result: CommandResult) -> bool:
    """Whether `codex` refused because this installation cannot bring a daemon up.

    A reading, not a failure: there is no daemon and cannot be one, which is UNREACHABLE
    rather than ERRORED. ERRORED means a daemon answered and reported its own link broken,
    and telling that to an owner whose install simply cannot serve one is the "button that
    could never explain itself" this feature exists to end.
    """
    return _UNSUPPORTED_INSTALL_SIGNATURE in result.stderr


@dataclass(frozen=True, slots=True, repr=False)
class CommandResult:
    """One finished `codex` invocation, as its runner observed it.

    Redacted like `config.TelegramSecrets.bot_token`, and for the identical reason its
    docstring gives: one uncaught traceback rendering its locals would print the value
    verbatim. `pair()` puts the manual pairing code into `stdout` on its way to a
    `PairingCode`, and `textual` renders rich tracebacks with frame locals -- so redacting
    only the final `PairingCode` would have been the last thirty centimetres of a pipe that
    is open along its whole length.
    """

    returncode: int
    stdout: str
    stderr: str

    def __repr__(self) -> str:
        return f"CommandResult(returncode={self.returncode}, stdout=<redacted>, stderr=<redacted>)"


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
        # `manualPairingCode` is what the CLI actually prints -- verified from
        # `codex app-server generate-ts --experimental` against the installed 0.151.0, whose
        # `RemoteControlPairingStartResponse` is camelCase throughout. The snake_case spelling
        # belongs to the relay *wire* protocol and never reaches stdout; it is still accepted
        # here because Codex's JSON is convention rather than contract (DEC-063) and the cost
        # of tolerating the other spelling is one `or`.
        #
        # The manual code is the one a human types into the app's manual-pairing screen; the
        # short `pairingCode` is the app-to-app handshake and pairs nothing when read aloud.
        # It is declared NULLABLE upstream: an enrollment offering no manual code must fail
        # closed here rather than render an empty box the owner would try to read out.
        code = payload.get("manualPairingCode") or payload.get("manual_pairing_code")
        expires_at = payload.get("expiresAt", payload.get("expires_at"))
        if not isinstance(code, str) or not code:
            raise ProtocolError("codex printed no manual pairing code")
        code = self._pairing_code(code)
        # `bool` is an `int` in Python, so an unchecked isinstance would accept `true` and
        # render a code that expired at 1970-01-01T00:00:01Z as merely "long expired".
        if isinstance(expires_at, bool) or not isinstance(expires_at, int | float):
            raise ProtocolError("codex printed a pairing code with no expiry")
        try:
            expiry = datetime.fromtimestamp(expires_at, tz=UTC)
        except (OverflowError, OSError, ValueError) as error:
            # NaN, +-1e30 and 10**30 each raise a different one of these. A caller that
            # already handles ProtocolError for every other failure of this boundary should
            # not also have to handle three arithmetic types to cover "the expiry is absurd".
            raise ProtocolError(
                "codex printed a pairing expiry this adapter cannot read"
            ) from error
        return PairingCode(code=code, expires_at=expiry)

    # ------------------------------------------------------------------ internals

    async def _enable(self) -> HostRemoteControlStatus:
        """Enable by the verb that is safe for the daemon state this host is actually in.

        **Which verb, and why it is a choice rather than a constant.** `remote-control start`
        is the obvious "on", and on a host whose daemon is already bootstrapped it is
        perfectly safe: it reaches `ensure_remote_control_started`, which flips the persisted
        preference and then calls `start()`, whose body contains no teardown at all -- it
        probes, reports `AlreadyRunning`, and leaves everything up.

        But that function has a second branch. On a host that is *not* bootstrapped it calls
        `bootstrap_locked`, and that one tears down a running managed backend before starting
        its own. Any pane attached to the backend it replaces exits on disconnect. So the
        naive constant argv reaches, in one real configuration, exactly the harm this module
        exists to avoid -- and it does so under a verb spelled `start`, which no banned-verb
        test could ever catch.

        The fix is to never put the CLI in a position to make that choice:

        - **A daemon is already running** -> `app-server daemon enable-remote-control`, the
          daemon-scoped verb documented as "enable remote control for future starts and a
          currently running managed daemon". It flips the preference on the live daemon and
          returns; it starts nothing and stops nothing, so no attached pane can be harmed.
        - **No daemon is running** -> `remote-control start --json`, which is what actually
          brings one up. Any bootstrap it performs has nothing to stop, because we just
          established nothing is listening.

        That makes the destructive branch **very nearly** unreachable, and the gap is worth
        stating rather than rounding off: the probe and the start are two separate execs, so a
        session launch landing between them can bring a backend up that the start then tears
        down. One spawn wide, and reachable by the sequence these surfaces teach -- turn it on
        with no daemon, then launch. Closing it needs a lock the `codex` CLI does not offer;
        BL-037 carries the decision. What this rule removes is the far larger case, where the
        branch was taken on *every* non-bootstrapped host, every time.

        Then report what the daemon says rather than what the CLI guessed.

        The envelope `remote-control start --json` prints is a *hint*, and only one arm of it
        is trustworthy on its own: a settled `connected` with `timedOut` false. Every other
        arm is answered by re-reading the daemon, which is the same move `_disable` already
        makes and for the same reason -- the authority on this host's enrollment is the
        daemon, not the exit status of the command that nudged it.

        Two arms motivate that, and neither is exotic:

        - `disabled` and `errored` arrive as a **non-zero exit with no JSON at all** (the CLI
          bails before its `--json` branch). Reporting a flat ERRORED there collapses "the
          preference is off" into "something went wrong", when a structured read would have
          said `disabled` -> INACTIVE authoritatively.
        - `connecting` routinely carries `timedOut: true`, because the CLI sets it whenever
          the status is still connecting on the daemon path. That means "enrolled, but the
          relay link did not come up while we waited" -- a real state, and one worth
          re-reading rather than rendering as a flat "on".

        The fallback is deliberately asymmetric: a re-read that answers DAEMON_ABSENT is
        **not** believed here, because we have just run the command whose whole job is to
        start that daemon. Believing it would render "off" for a machine that may well be
        enrolled and reachable, which is the one direction of wrongness that matters.
        """
        if await self._no_daemon_is_listening():
            result = await self._runner.run(
                REMOTE_CONTROL_ARGV["enable_when_absent"], timeout=_ENABLE_TIMEOUT_SECONDS
            )
        else:
            result = await self._runner.run(
                REMOTE_CONTROL_ARGV["enable_when_running"], timeout=_ENABLE_TIMEOUT_SECONDS
            )
        if _cannot_start_a_daemon(result):
            return HostRemoteControlStatus.observed(HostConnection.UNREACHABLE, server_name=None)
        # The daemon-scoped verb answers with JSON about the *preference* it wrote, not
        # about whether anything is serving it, so it never yields an
        # envelope; the re-read below is the whole answer in that branch.
        payload = self._payload(result.stdout) if result.returncode == 0 else None
        hint: HostRemoteControlStatus | None = None
        if payload is not None:
            try:
                hint = self._reading(payload)
            except ProtocolError:
                hint = None
            if (
                hint is not None
                and hint.connection is HostConnection.CONNECTED
                and not payload.get("timedOut")
            ):
                return hint

        try:
            confirmed = await self.status()
        except ProtocolError:
            confirmed = None
        if confirmed is not None and confirmed.connection is not HostConnection.DAEMON_ABSENT:
            return confirmed
        return hint or HostRemoteControlStatus.observed(HostConnection.ERRORED, server_name=None)

    async def _disable(self) -> HostRemoteControlStatus:
        result = await self._runner.run(
            REMOTE_CONTROL_ARGV["disable"], timeout=_DISABLE_TIMEOUT_SECONDS
        )
        if _cannot_start_a_daemon(result):
            # Measured NOT to happen on the npm build, where this verb succeeds. Classified
            # here anyway: the message means "no daemon can exist", whichever verb reports it.
            return HostRemoteControlStatus.observed(HostConnection.UNREACHABLE, server_name=None)
        if result.returncode != 0:
            return HostRemoteControlStatus.observed(HostConnection.ERRORED, server_name=None)
        # Re-read rather than trust this command's own answer. It does print JSON -- an
        # earlier version of this comment said "human-readable text only", which was wrong --
        # but what it reports is the *preference* it just wrote, and the reading both surfaces
        # show is whether anything is serving that preference. On a host that cannot start a
        # daemon those are different answers, and the daemon is the honest source.
        return await self.status()

    async def _no_daemon_is_listening(self) -> bool:
        """Tell "nothing is running" apart from "the daemon is up and failing"."""
        result = await self._runner.run(
            REMOTE_CONTROL_ARGV["daemon_probe"], timeout=_PROBE_TIMEOUT_SECONDS
        )
        if result.returncode == 0:
            return False
        if _ABSENT_DAEMON_SIGNATURE not in result.stderr:
            return False
        return any(cause in result.stderr for cause in _ABSENT_DAEMON_CAUSES)

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
        except (json.JSONDecodeError, ValueError, RecursionError):
            # RecursionError because deeply nested JSON raises it straight through the
            # decoder; unreadable is unreadable, whichever way the text was unreadable.
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _pairing_code(raw: str) -> str:
        """Bound and validate the secret, failing closed rather than sanitising it.

        The far less sensitive `serverName` two methods below already passes the presentation
        boundary (DEC-014); the code -- which is rendered into a Telegram message and a TUI
        modal -- went straight from `json.loads` to the domain type unbounded and unchecked.
        A `manualPairingCode` of `"\x1b]0;pwned\x07" + "A" * 100_000 + "\u202eEVIL"` is legal
        JSON and would have arrived intact.

        **Refused rather than cleaned**, which is the opposite of the choice made for the
        server name, and deliberately so: a scrubbed machine name is still a usable label,
        but a scrubbed *secret* is a secret that no longer works -- shown to an owner who
        would type it, watch it fail, and have no way to tell a mangled code from an expired
        one. Anything that is not a plausible code is therefore not a code.

        `isprintable()` is the same filter `probe_version_line` uses, and it is doing more
        than it looks: it is False for every Cc and Cf code point, so it rejects ESC and with
        it every ANSI sequence, plus U+202E RIGHT-TO-LEFT OVERRIDE and U+200B ZERO WIDTH
        SPACE -- the ones a reader would not think to check for.
        """
        cleaned = encodable_text(raw)
        if len(cleaned) > _PAIRING_CODE_MAX_CHARACTERS:
            raise ProtocolError("codex printed a pairing code far longer than a code can be")
        if not cleaned.isprintable() or cleaned.strip() != cleaned:
            raise ProtocolError("codex printed a pairing code that is not typable text")
        return cleaned

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
        # The sanitizer strips ESC and everything below space, which stops ANSI. It does not
        # stop C1 controls, bidi overrides or zero-width characters, and a RIGHT-TO-LEFT
        # OVERRIDE in a provider-controlled machine name reverses the rest of the rendered
        # row. `isprintable()` is False for every Cc and Cf code point, so the two together
        # cover what either alone leaves through (the `probe_version_line` precedent).
        cleaned = "".join(character for character in cleaned if character.isprintable())
        return cleaned or None


class AsyncCommandRunner:
    """Run one prevalidated `codex` vector without a shell, bounded by a timeout."""

    async def run(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult:
        try:
            return await self._run(argv, timeout=timeout)
        except FileNotFoundError:
            # `codex` is not on PATH. A caller that already handles ProtocolError for every
            # other way this boundary can fail should not have to also handle an OSError to
            # cover "the provider is not installed" -- the boundary answers in one vocabulary.
            raise ProtocolError("codex is not installed on this host") from None

    async def _run(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult:
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
