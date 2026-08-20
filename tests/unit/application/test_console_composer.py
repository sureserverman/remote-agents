"""The console composer keeps tabs equal to live sessions, and never touches lifecycle.

Everything here is presentation over Stage 1's validated operations: `ensure()` makes the
console exist with the dashboard as window 0 and the jump-home binding installed; `sync()`
links a tab per RUNNING/STARTING session and unlinks tabs whose session is gone; `open()`
prefers selecting the linked tab — the client stays in the console session, where the tab
bar and the jump-home binding mean something — and falls back to a direct client switch.
The one hard rule is DEC-006's: console failure degrades to nothing, it never raises into
a path that manages sessions, and the composer never writes a record of any kind.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from remote_agents.application.console import CONSOLE_BINDINGS, ConsoleComposer
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.console import ConsoleBindingAction, HostedPane
from remote_agents.ports.terminal import TerminalTargetMissing

_RUNNING = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_STARTING = SessionId.parse("11234567-89ab-cdef-0123-456789abcdef")
_ENDED = SessionId.parse("21234567-89ab-cdef-0123-456789abcdef")


def _record(session_id: SessionId, state: SessionState) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


class RecordingConsole:
    def __init__(
        self,
        *,
        exists: bool = True,
        windows: tuple[tuple[int, SessionId | None], ...] = ((0, None),),
        error: Exception | None = None,
    ) -> None:
        self.exists = exists
        self.windows = windows
        self.error = error
        self.active_window: int | None = 2
        self.calls: list[tuple] = []

    def _raise_if_armed(self) -> None:
        if self.error is not None:
            raise self.error

    async def console_exists(self) -> bool:
        self.calls.append(("console_exists",))
        self._raise_if_armed()
        return self.exists

    async def create_console(self, command: tuple[str, ...], cwd: Path) -> None:
        self.calls.append(("create_console", command, cwd))
        self._raise_if_armed()
        self.exists = True

    async def install_console_binding(
        self, key: str, action, command: tuple[str, ...] = ()
    ) -> None:
        self.calls.append(("install_console_binding", key, action, command))
        self._raise_if_armed()

    async def console_windows(self) -> tuple[tuple[int, SessionId | None], ...]:
        self.calls.append(("console_windows",))
        self._raise_if_armed()
        return self.windows

    async def pane_arrangement(self) -> tuple[HostedPane, ...]:
        """A console already at rest: its left slot holds the marked projects surface.

        Answered rather than omitted, because `settle` repairs a missing surface mark and
        a double that raised here would have that repair swallowed — leaving these tests
        green on an exception rather than on the behaviour they name.
        """
        self.calls.append(("pane_arrangement",))
        self._raise_if_armed()
        return (HostedPane(None, True, 0, 0, "%0", None, True),)

    async def mark_console_surface(self, pane_id: str) -> None:
        self.calls.append(("mark_console_surface", pane_id))
        self._raise_if_armed()

    async def link_session_window(self, session_id: SessionId) -> None:
        self.calls.append(("link_session_window", session_id))
        self._raise_if_armed()
        self.windows = (*self.windows, (len(self.windows), session_id))

    async def unlink_console_window(self, index: int) -> None:
        self.calls.append(("unlink_console_window", index))
        self._raise_if_armed()

    async def select_console_window(self, index: int) -> None:
        self.calls.append(("select_console_window", index))
        self._raise_if_armed()

    async def switch_client_to_session(self, session_id: SessionId) -> None:
        self.calls.append(("switch_client_to_session", session_id))
        self._raise_if_armed()

    async def console_active_window(self) -> int | None:
        self.calls.append(("console_active_window",))
        self._raise_if_armed()
        return self.active_window

    async def display_message(self, text: str) -> None:
        self.calls.append(("display_message", text))
        self._raise_if_armed()


#: What the projects key runs. The composition root supplies the real one; this is its shape.
_PROJECTS_COMMAND = ("remote-agents", "console", "projects")


def _composer(console: RecordingConsole) -> ConsoleComposer:
    return ConsoleComposer(
        console, ("remote-agents", "tui"), Path("/tmp"), projects_command=_PROJECTS_COMMAND
    )


def named(console: RecordingConsole, name: str) -> list[tuple]:
    return [call for call in console.calls if call[0] == name]


async def test_ensure_creates_the_console_only_when_it_is_missing() -> None:
    absent = RecordingConsole(exists=False)
    assert await _composer(absent).ensure() is True
    assert len(named(absent, "create_console")) == 1

    present = RecordingConsole(exists=True)
    assert await _composer(present).ensure() is True
    assert named(present, "create_console") == []


# --- The key budget (Sub-plan 3, Task 2.1) --------------------------------------------
#
# Every root binding is a key the agent can never receive, on every session, forever. That
# makes the *size* of this set a decision rather than an implementation detail, so these
# assert the declared budget itself and not merely that installing works.


async def test_ensure_installs_exactly_the_declared_binding_budget() -> None:
    console = RecordingConsole()
    await _composer(console).ensure()

    installed = [(call[1], call[2]) for call in named(console, "install_console_binding")]
    assert installed == [(binding.key, binding.action) for binding in CONSOLE_BINDINGS]


async def test_the_binding_budget_is_two_keys_and_every_one_of_them_says_why() -> None:
    """A third root binding should have to be argued for here, not appear silently.

    The plan allows the projects key plus *at most* two for pane focus. Two is what the
    layout actually needs: one key returns the projects surface to the left slot, and one
    cycles focus, which reaches any of three panes in at most two presses. A per-pane focus
    key would be a third key spent on a second way to do the same thing.
    """
    assert len(CONSOLE_BINDINGS) == 2
    assert len({binding.key for binding in CONSOLE_BINDINGS}) == 2, "two keys, not one twice"
    assert len({binding.action for binding in CONSOLE_BINDINGS}) == 2
    for binding in CONSOLE_BINDINGS:
        assert binding.why.strip(), f"{binding.key} is spent forever and does not say why"


async def test_the_projects_binding_carries_our_own_command_and_focus_carries_none() -> None:
    """The route back from a displayed agent is a command, because a key cannot exchange panes.

    tmux can select a window on its own; it cannot read our pane marks and decide which
    exchange returns the surface. So the projects key runs *our* program, and it is the
    composition root's to supply — the same argument `create_console` already takes, for the
    same reason: which entry point is the console is composition policy, not adapter shape.
    """
    console = RecordingConsole()
    await _composer(console).ensure()

    by_action = {call[2]: call[3] for call in named(console, "install_console_binding")}
    assert by_action[ConsoleBindingAction.SHOW_PROJECTS] == _PROJECTS_COMMAND
    assert by_action[ConsoleBindingAction.FOCUS_NEXT_PANE] == ()


async def test_re_ensure_does_not_stack_the_bindings() -> None:
    console = RecordingConsole()
    composer = _composer(console)
    await composer.ensure()
    await composer.ensure()

    # One bind per key per ensure; tmux overwrites a rebound key, so re-ensure cannot stack.
    installed = named(console, "install_console_binding")
    assert len(installed) == 2 * len(CONSOLE_BINDINGS)
    assert {call[1] for call in installed} == {binding.key for binding in CONSOLE_BINDINGS}


async def test_a_binding_that_cannot_be_installed_still_leaves_a_usable_console() -> None:
    """DEC-006 as it reaches presentation: a missing key costs the owner a key, never a console."""
    console = RecordingConsole()

    async def refuse(key, action, command=()):
        raise RuntimeError("tmux refused the bind")

    console.install_console_binding = refuse  # type: ignore[method-assign]
    assert await _composer(console).ensure() is False


async def test_sync_links_live_sessions_and_unlinks_gone_ones() -> None:
    console = RecordingConsole(windows=((0, None), (1, _ENDED)))
    records = (
        _record(_RUNNING, SessionState.RUNNING),
        _record(_STARTING, SessionState.STARTING),
        _record(_ENDED, SessionState.ENDED),
    )
    await _composer(console).sync(records)
    linked = {call[1] for call in named(console, "link_session_window")}
    assert linked == {_RUNNING, _STARTING}
    assert named(console, "unlink_console_window") == [("unlink_console_window", 1)]


async def test_sync_is_idempotent_over_an_already_correct_console() -> None:
    console = RecordingConsole(windows=((0, None), (1, _RUNNING)))
    await _composer(console).sync((_record(_RUNNING, SessionState.RUNNING),))
    assert named(console, "link_session_window") == []
    assert named(console, "unlink_console_window") == []


async def test_an_unattributable_tab_is_left_alone() -> None:
    """A window somebody created by hand carries no mark; it is not ours to remove."""
    console = RecordingConsole(windows=((0, None), (3, None)))
    await _composer(console).sync(())
    assert named(console, "unlink_console_window") == []


async def test_a_raising_console_degrades_to_nothing_and_raises_into_no_caller() -> None:
    broken = RecordingConsole(error=RuntimeError("tmux exploded"))
    composer = _composer(broken)
    assert await composer.ensure() is False
    await composer.sync((_record(_RUNNING, SessionState.RUNNING),))  # must not raise


async def test_a_gone_tab_during_unlink_is_already_what_sync_wanted() -> None:
    console = RecordingConsole(windows=((0, None), (2, _ENDED)))
    console.error = None

    async def unlink(index: int) -> None:
        console.calls.append(("unlink_console_window", index))
        raise TerminalTargetMissing("managed target is gone: ra-console:2")

    console.unlink_console_window = unlink  # type: ignore[method-assign]
    await _composer(console).sync(())  # must not raise


async def test_open_selects_the_linked_tab_so_the_client_stays_in_the_console() -> None:
    console = RecordingConsole(windows=((0, None), (2, _RUNNING)))
    await _composer(console).open(_RUNNING)
    assert named(console, "select_console_window") == [("select_console_window", 2)]
    assert named(console, "switch_client_to_session") == []


async def test_open_links_first_when_the_tab_is_missing() -> None:
    console = RecordingConsole(windows=((0, None),))
    await _composer(console).open(_RUNNING)
    assert len(named(console, "link_session_window")) == 1
    assert len(named(console, "select_console_window")) == 1


async def test_open_falls_back_to_a_direct_switch_when_tabs_fail() -> None:
    console = RecordingConsole()

    async def windows() -> tuple[tuple[int, SessionId | None], ...]:
        console.calls.append(("console_windows",))
        raise RuntimeError("listing failed")

    console.console_windows = windows  # type: ignore[method-assign]
    await _composer(console).open(_RUNNING)
    assert named(console, "switch_client_to_session") == [
        ("switch_client_to_session", _RUNNING)
    ]


async def test_flash_is_suppressed_while_the_owner_is_looking_at_the_dashboard() -> None:
    """Window 0 means the client rests on the dashboard, where the feed pane already
    shows the same news — flashing there would say one thing twice on one screen."""
    console = RecordingConsole()
    console.active_window = 0
    await _composer(console).flash("the agent is waiting for an answer")
    assert named(console, "display_message") == []

    console.active_window = 3
    await _composer(console).flash("the agent is waiting for an answer")
    assert named(console, "display_message") == [
        ("display_message", "the agent is waiting for an answer")
    ]


async def test_a_failing_flash_is_silence_never_an_exception() -> None:
    broken = RecordingConsole(error=RuntimeError("no server"))
    await _composer(broken).flash("news")  # must not raise
