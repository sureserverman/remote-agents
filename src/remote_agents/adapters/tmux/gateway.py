"""Dedicated-socket tmux inventory and ownership boundary."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from remote_agents.adapters.tmux.codec import (
    PANE_FORMAT,
    ManagedPane,
    console_target,
    current_console_window_args,
    display_message_args,
    exact_pane_target,
    exact_session_target,
    is_console_view,
    link_window_args,
    list_console_windows_args,
    pane_mark_args,
    parse_console_window,
    parse_pane,
    select_window_args,
    switch_client_args,
    switch_client_console_args,
    unlink_window_args,
    window_session_mark_args,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.ports.terminal import TerminalTargetMissing


class TmuxRunner(Protocol):
    """Argument-vector subprocess boundary used by the tmux adapter."""

    async def run(self, *argv: str) -> str: ...


@dataclass(frozen=True, slots=True)
class OrphanEvidence:
    """Read-only evidence for a pane that cannot be trusted as managed."""

    raw: str
    reason: str


_ABSENT_SERVER_SIGNATURES = ("no server running on", "error connecting to")
_ABSENT_TARGET_SIGNATURES = ("can't find session", "can't find pane", "session not found")

# tmux key names this adapter will bind: bare keys and function keys, optionally behind one
# C-/M- modifier. Deliberately narrower than what tmux accepts — a key is configuration, not
# input, and the closed shape keeps shell metacharacters out of the argv by construction.
_BINDABLE_KEY_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
)


def _validate_binding_key(key: str) -> str:
    body = key
    for modifier in ("C-", "M-"):
        if body.startswith(modifier):
            body = body.removeprefix(modifier)
            break
    if not body or not set(body) <= _BINDABLE_KEY_CHARACTERS:
        raise ValueError(
            "console binding key must be alphanumeric, optionally behind one C- or M- modifier"
        )
    return key


def _reports_absent_server(message: str) -> bool:
    """Recognize the dedicated server simply not being up, which means zero panes."""
    return any(signature in message for signature in _ABSENT_SERVER_SIGNATURES)


def _reports_absent_target(message: str) -> bool:
    """Recognize one named target being gone while the dedicated server still runs.

    A pane killed on its own — an OOM kill of its scope, say — leaves the server up and
    every other pane intact, so this is narrower than an absent server and has to be
    told apart from it: the server answering "that session is gone" is trustworthy
    evidence, whereas a server that cannot be reached says nothing about one target.
    """
    return any(signature in message for signature in _ABSENT_TARGET_SIGNATURES)


def _target_missing_or(error: RuntimeError, target: str) -> RuntimeError:
    """Retype a single-target failure that only means the target is already gone.

    Both an absent target and an absent server answer a single-target call the same way,
    because the dedicated server holds every managed pane: if it is not running, the pane
    it would have held is not either. Anything else is a real failure and keeps its type,
    so a broken tmux is never mistaken for an ended session.
    """
    message = str(error)
    if _reports_absent_target(message) or _reports_absent_server(message):
        return TerminalTargetMissing(f"managed target is gone: {target}")
    return error


def _decoded_lines(output: str) -> Iterator[tuple[str, ManagedPane | str | None]]:
    """Decode a `list-panes -a` body once, and let each caller apply its own policy.

    Yields `(line, ManagedPane)` for a trusted pane, `(line, reason)` for one that would not
    decode, and `(line, None)` for a line that is not evidence at all — blank, or the
    console's own view of itself. A console line is presentation exactly when it carries no
    managed mark; a *marked* pane hosted by the console is a displaced agent and comes back
    as evidence.

    Shared rather than written twice because the two callers want different things from the
    same reading — `inventory` keeps one pane per session and quarantines the rest, while
    `_claiming_panes` deliberately keeps them all — and both of this task's Critical findings
    lived in exactly these skip rules. Two copies of them is two chances to fix one and miss
    the other, and the bug that produces is a kill landing on the wrong pane.
    """
    for line in output.splitlines():
        if not line or is_console_view(line):
            yield line, None
            continue
        try:
            yield line, parse_pane(line)
        except ValueError as error:
            yield line, str(error)


@dataclass(frozen=True, slots=True)
class TmuxInventory:
    """Trusted managed panes and quarantined evidence from one dedicated server."""

    managed: tuple[ManagedPane, ...]
    orphans: tuple[OrphanEvidence, ...]


class TmuxGateway:
    """Forbid default-server and broad-target paths before subprocess execution."""

    def __init__(
        self,
        socket_name: str,
        runner: TmuxRunner,
        *,
        intent_directory: Path = Path("/var/lib/remote-agents/intents"),
    ) -> None:
        if socket_name != "remote-agents" and not socket_name.startswith("remote-agents-test-"):
            raise ValueError("a dedicated socket name is required")
        self._socket_name = socket_name
        self._runner = runner
        self._intent_directory = intent_directory

    async def inventory(self) -> TmuxInventory:
        """List panes only on the dedicated socket and quarantine malformed tags.

        A dedicated server that is not running holds no managed panes, which is an answer.
        Any other failure is raised rather than reported as an empty server, because a
        caller cannot tell the two apart and one of them means every session has ended.
        """
        try:
            output = await self._runner.run(
                *self._base_argv(), "list-panes", "-a", "-F", PANE_FORMAT
            )
        except RuntimeError as error:
            if _reports_absent_server(str(error)):
                return TmuxInventory((), ())
            raise
        managed: list[ManagedPane] = []
        orphans: list[OrphanEvidence] = []
        for line, decoded in _decoded_lines(output):
            if decoded is None:
                continue
            if isinstance(decoded, str):
                orphans.append(OrphanEvidence(line, decoded))
                continue
            pane = decoded
            # One observation per session identity — reconciliation keys evidence by
            # session. Every session this service launches is single-window, so a second
            # valid line for a known identity is either a repeat listing (dropped, first
            # wins) or a hand-grown extra window whose liveness *disagrees* with the first
            # — and a disagreement is ambiguous evidence, quarantined where a reader can
            # see it, never silently resolved in either direction.
            #
            # The pane id is what tells those two apart, and it has to now: a pane-scoped
            # mark is intrinsic to the pane, so tmux's re-listing of one pane under another
            # session carries the identity too, where a session-scoped mark never did. Same
            # pane id means one pane seen twice, and one pane cannot be in two states, so a
            # difference between two listings of it is a listing artifact rather than a
            # second window. Only *distinct* panes claiming one session are ambiguous.
            #
            # Keyed on the pane id alone. The liveness comparison this replaces was doing two
            # wrong things at once: on a repeat listing it was redundant, since one physical
            # pane cannot disagree with itself inside a single snapshot; and on two *distinct*
            # panes it silently swallowed the case where both happen to be alive — a
            # hand-grown second window whose evidence then vanished with nothing quarantined
            # for a reader to see, which is precisely the ambiguous half of DEC-020.
            earlier = next((p for p in managed if p.session_id == pane.session_id), None)
            if earlier is not None:
                if pane.pane_id != earlier.pane_id:
                    orphans.append(OrphanEvidence(line, "duplicate session evidence disagrees"))
                continue
            managed.append(pane)
        return TmuxInventory(tuple(managed), tuple(orphans))

    async def pane_for(self, session_id: SessionId) -> str | None:
        """Return the pane id currently carrying one identity, or None if none does.

        Asked fresh on every call and never cached, because the pane is the thing that
        moves: an answer taken once at launch is right until the first exchange and quietly
        wrong afterwards, which is the whole failure this addressing exists to remove.

        `None` has two causes that deliberately share an answer — a session marked under
        schema 1, whose identity lives on the session and names no pane, and a session that
        is gone. Both mean the same thing to a caller: there is no pane here to address. A
        third case never reaches here: two panes claiming one identity are resolved a level
        up, in `inventory`, which keeps one and quarantines the other (DEC-020), so this
        method never chooses between candidates.

        **Liveness is deliberately not a filter.** A PRESERVED pane still carries its mark
        and still resolves, because its retained output is the thing PRESERVED exists to
        keep and `capture` must reach it (DEC-021). The cost is that callers own the
        liveness question: verified against tmux 3.4, `send-keys` at a dead pane exits 0 and
        does nothing, so a caller that types without checking gets a success for a keystroke
        that went nowhere — which is exactly the never-sent stop DEC-022 requires be told
        apart from a sent one.
        """
        inventory = await self.inventory()
        return next(
            (
                pane.pane_id
                for pane in inventory.managed
                if pane.session_id == session_id and pane.pane_scoped
            ),
            None,
        )

    async def mutate(self, operation: str, session_name: str) -> str:
        """Run the one supported destructive operation against an exact managed target.

        **No longer the lifecycle stop.** `destroy` is, because a stop has to follow the
        agent's pane rather than the window it started in. What is left here is the
        session-level kill itself — the operation that removes a whole managed session by
        name — kept because the closed `operation` check is a DEC-001 guard on the only
        generic entry point this adapter has, and removing the method would not remove the
        need for that shape. Reach for `destroy` for anything that means "stop this agent".
        """
        if operation != "kill-session":
            raise ValueError("forbidden tmux operation")
        try:
            return await self._runner.run(
                *self._base_argv(), operation, "-t", exact_session_target(session_name)
            )
        except RuntimeError as error:
            raise _target_missing_or(error, session_name) from error

    async def _following_target(self, session_id: SessionId) -> str:
        """Return the target that follows this agent: its pane if it has one, else its session.

        The one place the two addressing schemes meet, so an operation cannot pick a scheme
        by accident and the fallback cannot drift between callers. A resolved pane id is
        exact and stays correct wherever the pane is hosted; the session target is what a
        schema-1 session has always used and still means the right thing, because a session
        marked under schema 1 has never been anywhere but its own window.

        The fallback is what keeps the upgrade continuous. Refusing to act on an
        unresolvable session would have stopped every already-running session working on the
        day this shipped, which is a worse failure than the one being fixed.

        A resolution that *fails* is a different answer from one that comes back without
        this pane, and it does **not** fall back. "I could not find out where the agent is"
        must never become "address its session target", because that target is a *window*
        target: tmux resolves it to whichever pane is there now, which after an exchange is
        not the agent. An earlier draft did fold the two together, reasoning that the
        operation would meet the same broken tmux one line later — but that only holds if
        the failure is symmetric, and a listing can fail transiently while a single-target
        call against the vacated window succeeds perfectly, against the wrong pane. On the
        `capture` path that misreads somebody else's screen; on the `send_keys` path it
        types into their terminal, and DEC-016 puts a bare Enter on that path.

        So the error is retyped and raised. A caller that could answer "already gone" still
        can — `_target_missing_or` keeps that signal — and a genuinely broken tmux still
        arrives as itself rather than as an ended session.
        """
        try:
            pane_id = await self.pane_for(session_id)
        except RuntimeError as error:
            raise _target_missing_or(error, f"ra-{session_id}") from error
        if pane_id is None:
            return exact_session_target(f"ra-{session_id}")
        return exact_pane_target(pane_id)

    async def capture(self, session_id: SessionId) -> str:
        """Capture only one exact managed pane without tmux escape-sequence output."""
        target = await self._following_target(session_id)
        try:
            return await self._runner.run(
                *self._base_argv(),
                "capture-pane",
                "-p",
                "-t",
                target,
            )
        except RuntimeError as error:
            raise _target_missing_or(error, f"ra-{session_id}") from error

    async def send_keys(self, session_id: SessionId, keys: tuple[str, ...]) -> None:
        """Send a profile-owned fixed key sequence to one exact managed target.

        Resolved **once for the whole sequence**, deliberately, and worth being precise
        about what that buys. A resolved pane id is a handle to one physical pane: every key
        in the loop reaches that same pane however many exchanges happen meanwhile, so the
        sequence cannot be split across two terminals. That protection belongs to the
        pane-id branch alone — the session-target fallback is a *window* target that tmux
        re-resolves on each `send-keys` subprocess, so a sequence addressed that way can
        still split if the window's occupant changes midway. Resolving once does not fix
        that and is not claimed to; what removes it is the pane id, which a schema-1 session
        does not have. The rest of what once-per-sequence buys is round-trips: one listing
        per stop rather than one per keystroke.
        """
        if not keys:
            raise ValueError("graceful stop requires a fixed key sequence")
        target = await self._following_target(session_id)
        for index, key in enumerate(keys):
            try:
                await self._runner.run(*self._base_argv(), "send-keys", "-t", target, key)
            except RuntimeError as error:
                # Retyped like every other single-target operation. A pane that vanishes
                # between two keys left the caller a raw RuntimeError, so a stop interrupted
                # by the agent exiting mid-sequence was indistinguishable from a broken
                # tmux — and DEC-022 turns on telling those apart.
                raise _target_missing_or(error, f"ra-{session_id}") from error
            if index < len(keys) - 1:
                await asyncio.sleep(0.15)

    async def _claiming_panes(self, session_id: SessionId) -> tuple[tuple[str, bool], ...]:
        """Every decoded pane claiming one identity, with whether the claim is its own.

        Deliberately *before* `inventory`'s one-per-session dedup. That dedup keeps whichever
        line tmux listed first and quarantines the rest as ambiguous evidence for a reader to
        resolve (DEC-020) — the right answer for observation, and the wrong basis for a kill,
        because "listed first" is an ordering, not an identification.
        """
        output = await self._runner.run(*self._base_argv(), "list-panes", "-a", "-F", PANE_FORMAT)
        claiming: dict[str, bool] = {}
        for _line, decoded in _decoded_lines(output):
            if isinstance(decoded, ManagedPane) and decoded.session_id == session_id:
                claiming.setdefault(decoded.pane_id, decoded.pane_scoped)
        return tuple(claiming.items())

    async def destroy(self, session_id: SessionId) -> None:
        """Destroy one agent by killing the pane it occupies, whatever is hosting it.

        The operation this whole addressing change was ordered around, and the one where
        `kill-session` is not merely imprecise but wrong. Verified on tmux 3.4 (2026-08-19):
        killing a session whose window is **linked into another session** removes the session
        name, exits 0, and leaves the pane — and the agent in it — running. The shipped
        console links a window for every live session, so `kill-session` on a session the
        owner had open as a tab reported success while the agent kept going: a record at
        ENDED over a live process, exactly what DEC-006 forbids. That is a bug this method
        fixes, not only one it prevents. `kill-pane` names the pane object, so it reaches the
        agent through a link or a swap, and takes nothing else with it.

        **Which pane, and how much a mark is allowed to say.** A schema-2 mark is the pane's
        own, so it identifies one pane and that pane is killed. A schema-1 mark is
        session-scoped, and tmux's pane → session fallback hands it to *every* pane in that
        session's window — verified: an operator's hand-split pane reports the agent's
        identity, and `split-window -b` puts it first in `list-panes -a`. So an inherited
        mark identifies the **session**, never a pane within it, and picking one of those
        panes would be picking by listing order: a draft of this method did exactly that and
        killed the operator's pane while the agent ran on. Every pane claiming by inheritance
        is therefore killed — the same set `kill-session` would have destroyed, minus its
        inability to reach a linked window.

        `kill-session` survives only for an identity with no decoded pane at all, where
        nothing narrower exists to name and the alternative to a wide kill is not stopping
        the agent.

        **Nothing chases the kill.** Probed: killing a session's last pane destroys the
        session, so the stranded-empty-session the task's second clause guarded against
        cannot exist. The only home session that survives is one still holding another pane —
        once the swap console lands, the projects surface parked there — and removing it
        would destroy a process this service never started.
        """
        try:
            claiming = await self._claiming_panes(session_id)
        except RuntimeError as error:
            raise _target_missing_or(error, f"ra-{session_id}") from error
        owned = [pane_id for pane_id, is_own in claiming if is_own]
        targets = owned or [pane_id for pane_id, _ in claiming]
        if not targets:
            try:
                await self._runner.run(
                    *self._base_argv(),
                    "kill-session",
                    "-t",
                    exact_session_target(f"ra-{session_id}"),
                )
            except RuntimeError as error:
                raise _target_missing_or(error, f"ra-{session_id}") from error
            return
        # Killing the first may take the window, and so the rest, with it — that is success,
        # not failure. Only a kill that reached nothing at all is worth raising about.
        missing: TerminalTargetMissing | None = None
        killed = False
        for pane_id in targets:
            try:
                await self._runner.run(
                    *self._base_argv(), "kill-pane", "-t", exact_pane_target(pane_id)
                )
                killed = True
            except RuntimeError as error:
                retyped = _target_missing_or(error, f"ra-{session_id}")
                if not isinstance(retyped, TerminalTargetMissing):
                    # Only "already gone" is tolerable here, and only because an earlier kill
                    # can take the window and its siblings with it. Any other failure is a
                    # tmux that did not do what it was told, and swallowing it because some
                    # *other* pane's kill happened to succeed would report a stop that did
                    # not reach the agent — the DEC-006 outcome, arriving through the error
                    # path instead of the targeting one.
                    raise retyped from error
                missing = missing or retyped
        if not killed and missing is not None:
            raise missing

    async def launch(
        self, session_id: SessionId, project_id: ProjectId, profile_id: ProfileId, cwd: Path
    ) -> None:
        """Create a tagged managed session that invokes only the fixed runner module."""
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ValueError("launch working directory must be an existing absolute directory")
        session_name = f"ra-{session_id}"
        target = exact_session_target(session_name)
        await self._runner.run(
            *self._base_argv(),
            "new-session",
            "-d",
            "-s",
            session_name,
            "-c",
            str(cwd),
            sys.executable,
            "-m",
            "remote_agents.adapters.tmux.session_runner",
            str(session_id),
            "--intent-dir",
            str(self._intent_directory),
        )
        await self._runner.run(
            *self._base_argv(), "set-option", "-t", target, "remain-on-exit", "on"
        )
        # Identity is stamped on the pane and nowhere else, so it travels with the agent and
        # leaves nothing behind for a later occupant of this window to inherit. The option
        # names live in the codec with the builder; this method knows only that it marks.
        for mark in pane_mark_args(session_id, project_id, profile_id):
            await self._runner.run(*self._base_argv(), *mark)

    async def console_exists(self) -> bool:
        """Ask whether the console session is present; an absent server is a plain no."""
        try:
            await self._runner.run(*self._base_argv(), "has-session", "-t", console_target())
        except RuntimeError as error:
            message = str(error)
            if _reports_absent_server(message) or _reports_absent_target(message):
                return False
            raise
        return True

    async def create_console(self, dashboard_command: tuple[str, ...], cwd: Path) -> None:
        """Create the detached console session running the dashboard as window 0.

        The command is the composition root's own program, passed as an argument vector
        rather than assembled here, because which entry point *is* the dashboard is
        composition policy — while everything about the session (name, socket, detachment)
        stays this adapter's closed shape.
        """
        if not dashboard_command:
            raise ValueError("the console needs a dashboard command")
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ValueError("console working directory must be an existing absolute directory")
        await self._runner.run(
            *self._base_argv(),
            "new-session",
            "-d",
            "-s",
            "ra-console",
            "-c",
            str(cwd),
            *dashboard_command,
        )

    async def link_session_window(self, session_id: SessionId) -> None:
        """Mark one managed session's window with its identity, then tab it into the console.

        The mark travels with the shared window object into the console's listing; the link
        appends at the console's next free index (tmux 3.4, verified). Order matters only in
        that an unmarked linked window would be a tab `console_windows` cannot attribute.
        """
        try:
            await self._runner.run(*self._base_argv(), *window_session_mark_args(session_id))
            await self._runner.run(*self._base_argv(), *link_window_args(session_id))
        except RuntimeError as error:
            raise _target_missing_or(error, f"ra-{session_id}") from error

    async def unlink_console_window(self, window_index: int) -> None:
        """Remove one console tab; the codec refuses index 0 so the dashboard cannot go."""
        try:
            await self._runner.run(*self._base_argv(), *unlink_window_args(window_index))
        except RuntimeError as error:
            raise _target_missing_or(error, f"ra-console:{window_index}") from error

    async def console_windows(self) -> tuple[tuple[int, SessionId | None], ...]:
        """List (index, owning session) per console window; no console means no windows."""
        try:
            output = await self._runner.run(*self._base_argv(), *list_console_windows_args())
        except RuntimeError as error:
            message = str(error)
            if _reports_absent_server(message) or _reports_absent_target(message):
                return ()
            raise
        return tuple(
            parse_console_window(line) for line in output.splitlines() if line
        )

    async def select_console_window(self, window_index: int) -> None:
        """Focus one console window by index, 0 being the dashboard."""
        try:
            await self._runner.run(*self._base_argv(), *select_window_args(window_index))
        except RuntimeError as error:
            raise _target_missing_or(error, f"ra-console:{window_index}") from error

    async def switch_client_to_session(self, session_id: SessionId) -> None:
        """Move the attached client to one exact managed session."""
        try:
            await self._runner.run(*self._base_argv(), *switch_client_args(session_id))
        except RuntimeError as error:
            raise _target_missing_or(error, f"ra-{session_id}") from error

    async def switch_client_to_console(self) -> None:
        """Move the attached client back to the console session."""
        try:
            await self._runner.run(*self._base_argv(), *switch_client_console_args())
        except RuntimeError as error:
            raise _target_missing_or(error, "ra-console") from error

    async def console_active_window(self) -> int | None:
        """The console's current window index, or None when there is nothing to ask."""
        try:
            output = await self._runner.run(*self._base_argv(), *current_console_window_args())
        except RuntimeError as error:
            message = str(error)
            if _reports_absent_server(message) or _reports_absent_target(message):
                return None
            raise
        try:
            return int(output.strip())
        except ValueError:
            return None

    async def display_message(self, text: str) -> None:
        """Flash one line on the status bar of whatever window the client is on."""
        await self._runner.run(*self._base_argv(), *display_message_args(text))

    async def install_console_binding(self, key: str) -> None:
        """Bind one validated root-table key, on this socket only, to reach the dashboard."""
        await self._runner.run(
            *self._base_argv(),
            "bind-key",
            "-n",
            _validate_binding_key(key),
            *select_window_args(0),
        )

    def _base_argv(self) -> tuple[str, str, str]:
        """Return the only valid tmux server selector for this adapter."""
        return ("tmux", "-L", self._socket_name)

    @property
    def intent_directory(self) -> Path:
        """Return the adapter-owned private directory supplied to the fixed runner."""
        return self._intent_directory
