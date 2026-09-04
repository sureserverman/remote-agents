"""Codex's host-level Remote Control, spoken as a closed table of `codex` argument vectors.

This is the one module in the project that runs a `codex` command whose effect reaches past a
pane: turning Remote Control on enrols this machine with OpenAI's relay, so the phone can
drive it. Everything about the shape here follows from that.

**The table is closed and it has one home.** Every vector is a module constant beginning with
`codex`; nothing is built from a caller's string, and the destructive teardown verb is absent
by construction rather than by discipline -- a test asserts no entry carries it.

**What "off" costs, measured rather than reasoned.** Turning Remote Control off is
`disable-remote-control`, and it *does* restart the daemon: across one call the app-server
process is replaced and `--remote-control` disappears from its argv. Two earlier versions of
this paragraph got the consequence wrong in opposite directions -- first "attached panes keep
going", then "an attached Codex TUI exits on disconnect" -- and running the live drill on a
standalone install settled it. The attached pane is **not** killed: same pid, still at a
usable prompt afterwards. What it loses is the conversation, durably: the TUI reconnects to
the replacement daemon and reports "This conversation is unavailable; no operation was sent."

So the cost of "off" is in-flight work, not the session, and there is no gentler verb to
switch to. An earlier note here proposed the daemon's own `remoteControl/disable` RPC; no such
client request exists in the app-server protocol. See BL-040, which supersedes BL-038.

**Both collaborators are injected.** The subprocess runner and the daemon-state reader are
constructor arguments, so nothing below `tests/live` spawns a real `codex` or touches a real
`CODEX_HOME`. The default implementations are at the bottom of this module.

**Nothing this module reads is echoed.** `codex` writes paths, auth hints and prompts to
stderr; a status line built out of that text renders whatever the provider happened to say
into a Telegram message. So a failure becomes a *reading* -- UNREACHABLE, "this project has no
answer" -- and never a string. `serverName` is the one provider value that is rendered, and it
passes the presentation-boundary encoder first (DEC-014).

**Reading is two local facts, because the protocol offers no third.** `status()` does not ask
the daemon anything, and the reason is worth stating rather than discovering again. The
original design asked it over `codex app-server proxy` for a `remoteControl/status/read`
method. That method does not exist -- `codex app-server generate-json-schema` defines no
`remoteControl` client request at all, only a `status/changed` server notification -- and the
proxy transport never answered `initialize` regardless, so the reading it produced was always
either a fallback or a raised error. The CLI offers no read-only status verb either: every
`app-server daemon` subcommand mutates something except `version`, which says nothing about
enrollment.

What is left is what Codex writes down for itself, and it is enough for the states an owner
acts on:

- **the persisted preference**, `$CODEX_HOME/app-server-daemon/settings.json` ->
  `remoteControlEnabled`, which Codex rewrites on every toggle (watched flipping both ways);
- **whether anything is serving it**, the existing `daemon version` probe, which answers in
  well under a second and starts nothing.

Preference off is DISABLED whatever the daemon is doing. Preference on with a daemon running
is CONNECTED; with none, DAEMON_ABSENT -- "on, but nothing is serving it", which is the whole
reason that member exists. Anything this module cannot read is UNREACHABLE, never a guess.

**What this read cannot see, stated so no surface claims otherwise.** Whether the link to
OpenAI's relay is actually healthy: a daemon can be up and enrolled and still unable to reach
the relay, and this reads CONNECTED. That was ERRORED's job, and ERRORED is now reachable only
from the enable command's own JSON, which does report it at the moment of enabling. Likewise
`serverName`: the settings file does not carry one, so a plain read supplies None and only
`set_state` can name the machine. The bot already renders the name conditionally.

**Reading is not enabling.** Nothing in `status()` starts a process other than the probe,
which starts no daemon; the preference is read from a file.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from remote_agents.adapters.agents.protocols import ProtocolError
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
#: a vector assembled from a caller's value is a vector a caller can steer. `daemon_probe`
#: answers whether anything is listening at all; there is no read vector, because the reading
#: comes from a file Codex maintains rather than from a command (see the module docstring).
#: The table carries no teardown verb and a test proves it.
#:
#: `status` lived here until 2026-09-03, pointing at `codex app-server proxy`. It was removed
#: rather than left unused: it named a transport that never once answered.
REMOTE_CONTROL_ARGV: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "daemon_probe": ("codex", "app-server", "daemon", "version"),
        "enable_when_absent": ("codex", "remote-control", "start", "--json"),
        "enable_when_running": ("codex", "app-server", "daemon", "enable-remote-control"),
        "disable": ("codex", "app-server", "daemon", "disable-remote-control"),
        "pair": ("codex", "remote-control", "pair", "--json"),
    }
)

#: What `codex remote-control start --json` calls each state, mapped to the connection
#: vocabulary the domain derives from. Closed: a value not listed here is a protocol this
#: adapter does not speak, and it raises rather than guessing at a neighbouring meaning.
#:
#: This table used to serve a `remoteControl/status/read` RPC as well. That method does not
#: exist in the app-server protocol and never did, so the *only* thing that speaks this
#: vocabulary now is the enable command's own envelope -- which is also the only place
#: `connecting` and `errored` can still come from.
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

#: Where Codex persists the enrollment preference, relative to `CODEX_HOME`.
#:
#: A file rather than a command because the CLI exposes no read-only status verb, and a fact
#: Codex writes for its own use rather than a documented interface (DEC-063: Codex facts are
#: convention). It is load-bearing, so it fails *closed*: an absent, unreadable or
#: unrecognisable file yields "no reading" -- UNREACHABLE -- and never "off". A future Codex
#: that moves this file therefore degrades to honest silence rather than to a confident wrong
#: answer on a machine that is enrolled.
#:
#: Watched flipping `true` -> `false` -> `true` in step with the toggle on 2026-09-03.
_SETTINGS_RELATIVE_PATH = ("app-server-daemon", "settings.json")

#: The key inside it. Codex writes exactly `{"remoteControlEnabled": <bool>}` today.
_PREFERENCE_KEY = "remoteControlEnabled"

#: A settings file is small. This bound is what stops a `CODEX_HOME` pointed at something
#: enormous -- by accident or otherwise -- from being read into memory to answer a toggle.
_SETTINGS_MAX_BYTES = 64 * 1024

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


class DaemonSettings(Protocol):
    """Read this host's persisted Remote Control preference, or say it cannot be read.

    Three-valued on purpose. `True` and `False` are answers; `None` means *no answer* -- the
    file is missing, unreadable, or does not say what this adapter knows how to read -- and it
    must never be collapsed into `False`, which would render "off" on a machine that is on.
    """

    async def remote_control_preference(self) -> bool | None: ...


class DaemonLiveness(StrEnum):
    """Whether anything is serving the preference, as the local probe could tell."""

    RUNNING = "running"
    """`daemon version` answered."""
    ABSENT = "absent"
    """It failed, naming the control socket, because the socket is not there (ENOENT)."""
    INDETERMINATE = "indeterminate"
    """It failed some other way -- EACCES, a divergent CODEX_HOME, a backlog refusal.

    Distinct from ABSENT because reading those as "nothing is listening" is how a machine
    that is on gets rendered off; that is the one direction of wrongness that matters here."""


class CodexRemoteControl:
    """Codex's `HostRemoteControl`: read the daemon, flip it, mint a pairing code."""

    def __init__(
        self, runner: CommandRunner | None = None, settings: DaemonSettings | None = None
    ) -> None:
        self._runner: CommandRunner = runner or AsyncCommandRunner()
        self._settings: DaemonSettings = settings or CodexHomeSettings()

    async def status(self) -> HostRemoteControlStatus:
        """Read the preference Codex persisted, then whether anything is serving it.

        Starts nothing and changes nothing: one file read and one `daemon version`. The
        module docstring carries why it is not a question put to the daemon.

        The order is deliberate. The preference is read first and answers on its own when it
        says *off*, because a running daemon without remote control enabled is genuinely off
        and there is nothing the probe could add. Only "on" needs the second fact, which is
        the difference between "the phone can reach this machine" and "it could, once
        something starts".

        Every unreadable path lands on UNREACHABLE rather than raising. The caller is a
        surface drawing a row, and a raised error there costs the owner the whole reading --
        including the part this did establish -- where UNREACHABLE says "no answer" in the
        vocabulary the row already renders. `ProtocolError` still escapes `set_state` and
        `pair`, which are actions and must be able to fail.
        """
        preference = await self._settings.remote_control_preference()
        if preference is None:
            return HostRemoteControlStatus.observed(HostConnection.UNREACHABLE, server_name=None)
        if not preference:
            return HostRemoteControlStatus.observed(HostConnection.DISABLED, server_name=None)
        try:
            liveness = await self._daemon_liveness()
        except ProtocolError:
            # `codex` is missing, or did not answer in time. Enrolled-but-unverifiable.
            return HostRemoteControlStatus.observed(HostConnection.UNREACHABLE, server_name=None)
        if liveness is DaemonLiveness.RUNNING:
            return HostRemoteControlStatus.observed(HostConnection.CONNECTED, server_name=None)
        if liveness is DaemonLiveness.ABSENT:
            return HostRemoteControlStatus.observed(HostConnection.DAEMON_ABSENT, server_name=None)
        return HostRemoteControlStatus.observed(HostConnection.UNREACHABLE, server_name=None)

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
        """Release anything this adapter holds open. Today that is nothing.

        It held a long-lived `codex app-server proxy` child until 2026-09-03, opened by the
        first `status()` and leaked once per construction without this. The read is now a file
        and a bounded subprocess that has already exited, so there is nothing left to reclaim.

        Kept, rather than deleted with the thing it closed, because it is the lifecycle hook
        `composition/service.py` calls in a `finally` on the way out: a collaborator that grows
        a resource later should find the hook already wired and already awaited by every
        caller. It stays tolerant of an injected double that has no `close`.
        """
        close = getattr(self._settings, "close", None)
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
        # Two readings are refused here rather than believed, and for the same reason: we have
        # just run the command whose whole job is to enrol this host, so a re-read saying
        # "nothing is listening" (DAEMON_ABSENT) or "I have no answer" (UNREACHABLE) is far
        # more likely to be a race with a daemon still coming up than the truth. Believing
        # either would render "off"/"unknown" over a hint that said `connected`, which is the
        # one direction of wrongness this feature cannot afford.
        #
        # UNREACHABLE joined DAEMON_ABSENT here on 2026-09-03: `status()` used to *raise* when
        # it could not read, and the `except` above turned that into "no opinion". It now
        # returns a reading instead, so without this the unreadable case would silently start
        # winning over the hint.
        unconvincing = {HostConnection.DAEMON_ABSENT, HostConnection.UNREACHABLE}
        if confirmed is not None and confirmed.connection not in unconvincing:
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
        """Whether nothing is listening -- the boolean `_enable` needs to pick its verb.

        Only ABSENT counts as "no daemon". INDETERMINATE deliberately answers False, so the
        enable path takes the daemon-scoped verb that starts nothing: on an ambiguous probe
        the safe assumption is that a daemon *might* be up, because the alternative verb can
        reach `bootstrap_locked` and tear a running backend down.
        """
        return await self._daemon_liveness() is DaemonLiveness.ABSENT

    async def _daemon_liveness(self) -> DaemonLiveness:
        """Tell "nothing is running" apart from "the daemon is up" and from "cannot tell".

        The three-way answer is the same evidence the boolean above was already weighing --
        `status()` needs the third arm, because "I could not find out" and "nothing is
        listening" render as different readings.
        """
        result = await self._runner.run(
            REMOTE_CONTROL_ARGV["daemon_probe"], timeout=_PROBE_TIMEOUT_SECONDS
        )
        if result.returncode == 0:
            return DaemonLiveness.RUNNING
        if _ABSENT_DAEMON_SIGNATURE not in result.stderr:
            return DaemonLiveness.INDETERMINATE
        if any(cause in result.stderr for cause in _ABSENT_DAEMON_CAUSES):
            return DaemonLiveness.ABSENT
        return DaemonLiveness.INDETERMINATE

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


class CodexHomeSettings:
    """Read `remoteControlEnabled` out of the daemon settings file under `CODEX_HOME`.

    Honours `CODEX_HOME` because `codex` does -- verified by pointing it at a temporary
    directory and watching the control socket move with it. Reading `~/.codex` unconditionally
    would, on a host whose service and shell disagree about it, report the wrong machine's
    preference with total confidence.

    **Every failure is `None`, never `False`.** Missing file, unreadable bytes, bad JSON, a
    non-object, a missing key, a non-boolean value, a file too large to be a settings file, or
    any OSError on the way -- all mean "no reading". This class exists to answer one question
    and it declines rather than guesses, because the guess it would otherwise make is "off",
    which is the answer an owner acts on by not acting.

    Synchronous file work on the event loop is deliberate: it is a single stat-and-read of a
    file measured in tens of bytes, and a thread hand-off would cost more than it saves.
    """

    def __init__(self, home: Path | None = None) -> None:
        self._home = home

    async def remote_control_preference(self) -> bool | None:
        path = self._settings_path()
        try:
            if path.stat().st_size > _SETTINGS_MAX_BYTES:
                return None
            raw = path.read_text(encoding="utf-8")
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError, RecursionError):
            return None
        if not isinstance(parsed, dict):
            return None
        value = parsed.get(_PREFERENCE_KEY)
        # `isinstance(True, int)` is True, so an unguarded numeric check would read a `1` as
        # enabled. Only an actual boolean is an answer here.
        return value if isinstance(value, bool) else None

    def _settings_path(self) -> Path:
        home = self._home
        if home is None:
            configured = os.environ.get("CODEX_HOME")
            home = Path(configured).expanduser() if configured else Path.home() / ".codex"
        return home.joinpath(*_SETTINGS_RELATIVE_PATH)
