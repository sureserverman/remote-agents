"""Dedicated-socket tmux inventory and ownership boundary."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from remote_agents.adapters.tmux.codec import (
    PANE_FORMAT,
    ManagedPane,
    console_target,
    current_console_window_args,
    display_message_args,
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
        for line in output.splitlines():
            if not line:
                continue
            # The console's view of the server is presentation, never evidence: its own
            # dashboard pane and its re-listing of every linked window are dropped before
            # decoding, so expected console noise cannot pollute the orphan quarantine.
            if is_console_view(line):
                continue
            try:
                pane = parse_pane(line)
            except ValueError as error:
                orphans.append(OrphanEvidence(line, str(error)))
                continue
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
            earlier = next((p for p in managed if p.session_id == pane.session_id), None)
            if earlier is not None:
                if pane.pane_id != earlier.pane_id and (pane.live, pane.preserved) != (
                    earlier.live,
                    earlier.preserved,
                ):
                    orphans.append(OrphanEvidence(line, "duplicate session evidence disagrees"))
                continue
            managed.append(pane)
        return TmuxInventory(tuple(managed), tuple(orphans))

    async def mutate(self, operation: str, session_name: str) -> str:
        """Run the one supported destructive operation against an exact managed target."""
        if operation != "kill-session":
            raise ValueError("forbidden tmux operation")
        try:
            return await self._runner.run(
                *self._base_argv(), operation, "-t", exact_session_target(session_name)
            )
        except RuntimeError as error:
            raise _target_missing_or(error, session_name) from error

    async def capture(self, session_id: SessionId) -> str:
        """Capture only one exact managed pane without tmux escape-sequence output."""
        try:
            return await self._runner.run(
                *self._base_argv(),
                "capture-pane",
                "-p",
                "-t",
                exact_session_target(f"ra-{session_id}"),
            )
        except RuntimeError as error:
            raise _target_missing_or(error, f"ra-{session_id}") from error

    async def send_keys(self, session_id: SessionId, keys: tuple[str, ...]) -> None:
        """Send a profile-owned fixed key sequence to one exact managed target."""
        if not keys:
            raise ValueError("graceful stop requires a fixed key sequence")
        target = exact_session_target(f"ra-{session_id}")
        for index, key in enumerate(keys):
            await self._runner.run(*self._base_argv(), "send-keys", "-t", target, key)
            if index < len(keys) - 1:
                await asyncio.sleep(0.15)

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
