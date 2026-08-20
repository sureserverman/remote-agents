"""The console composer arranges panes and keys, and never touches lifecycle.

Everything here is presentation over validated tmux operations: `ensure()` makes the console
exist as one window of three marked panes and installs the key budget; `_build_panes` rebuilds
exactly the pane that died and declines the two states it must not guess at; `open()` shows a
session by *exchanging* the console's left pane with that agent's own. The one hard rule is
DEC-006's: console failure degrades to nothing, it never raises into a path that manages
sessions, and the composer writes no record of any kind.

**This file used to describe tabs** — `sync()` linking one per live session, `open()` selecting
the linked window and falling back to a client switch. That mechanism retired with Task 2.4;
what survives of `sync` is noticing what the other writer did to the session on screen.
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
from remote_agents.ports.console import (
    ConsoleBindingAction,
    ConsolePaneSlot,
    HostedPane,
)

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
        arrangement: tuple[HostedPane, ...] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.exists = exists
        self.windows = windows
        self._arrangement = arrangement
        self._next_pane = 90
        self.error = error
        self.zoomed_pane: str | None = "%0"
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
        self._arrangement = (
            HostedPane(
                host=None,
                on_console=True,
                window_index=0,
                pane_index=0,
                pane_id="%0",
                session_id=None,
            ),
        )

    async def split_console_pane(
        self,
        target_pane: str,
        command: tuple[str, ...],
        cwd: Path,
        *,
        vertical: bool,
        percent: int,
        before: bool = False,
    ) -> str:
        self._raise_if_armed()
        self._next_pane += 1
        pane_id = f"%{self._next_pane}"
        self.calls.append(
            (
                "split_console_pane",
                target_pane,
                command,
                cwd,
                vertical,
                percent,
                pane_id,
                before,
            )
        )
        return pane_id

    async def normalize_console_layout(
        self, main_percent: int, minor_pane: str, minor_percent: int
    ) -> None:
        self.calls.append(("normalize_console_layout", main_percent, minor_pane, minor_percent))
        self._raise_if_armed()

    async def mark_console_slot(self, pane_id: str, slot) -> None:
        self.calls.append(("mark_console_slot", pane_id, slot))
        self._raise_if_armed()
        self._arrangement = tuple(
            (
                HostedPane(
                    host=pane.host,
                    on_console=pane.on_console,
                    window_index=pane.window_index,
                    pane_index=pane.pane_index,
                    pane_id=pane.pane_id,
                    session_id=pane.session_id,
                    surface=pane.surface,
                    console_slot=slot.value,
                )
                if pane.pane_id == pane_id
                else pane
            )
            for pane in (self._arrangement or ())
        )

    async def install_console_binding(
        self, key: str, action, command: tuple[str, ...] = ()
    ) -> None:
        self.calls.append(("install_console_binding", key, action, command))
        self._raise_if_armed()


    async def pane_arrangement(self) -> tuple[HostedPane, ...]:
        """A console already at rest: its left slot holds the marked projects surface.

        Answered rather than omitted, because `settle` repairs a missing surface mark and
        a double that raised here would have that repair swallowed — leaving these tests
        green on an exception rather than on the behaviour they name.
        """
        self.calls.append(("pane_arrangement",))
        self._raise_if_armed()
        if self._arrangement is not None:
            return self._arrangement
        return (HostedPane(None, True, 0, 0, "%0", None, True, ConsolePaneSlot.PROJECTS.value),)





    async def swap_panes(self, source_pane: str, target_pane: str) -> None:
        self.calls.append(("swap_panes", source_pane, target_pane))
        self._raise_if_armed()

    async def console_zoomed_pane(self) -> str | None:
        self.calls.append(("console_zoomed_pane",))
        self._raise_if_armed()
        return self.zoomed_pane

    async def display_message(self, text: str) -> None:
        self.calls.append(("display_message", text))
        self._raise_if_armed()


#: What the projects key runs. The composition root supplies the real one; this is its shape.
_PROJECTS_COMMAND = ("remote-agents", "console", "projects")

#: One command per pane, exactly as the composition root supplies them.
_PANE_COMMANDS = {
    ConsolePaneSlot.PROJECTS: ("remote-agents", "pane", "projects"),
    ConsolePaneSlot.SESSIONS: ("remote-agents", "pane", "sessions"),
    ConsolePaneSlot.FEED: ("remote-agents", "pane", "feed"),
}


def _three_pane_console() -> tuple[HostedPane, ...]:
    """The console at rest: projects left, sessions right-top, feed right-bottom."""
    return tuple(
        HostedPane(
            host=None,
            on_console=True,
            window_index=0,
            pane_index=index,
            pane_id=f"%{index}",
            session_id=None,
            surface=slot is ConsolePaneSlot.PROJECTS,
            console_slot=slot.value,
        )
        for index, slot in enumerate(
            (ConsolePaneSlot.PROJECTS, ConsolePaneSlot.SESSIONS, ConsolePaneSlot.FEED)
        )
    )


def _composer(console: RecordingConsole) -> ConsoleComposer:
    return ConsoleComposer(
        console,
        ("remote-agents", "tui"),
        Path("/tmp"),
        projects_command=_PROJECTS_COMMAND,
        pane_commands=_PANE_COMMANDS,
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


# --- The console window is three panes (Sub-plan 3, Task 2.2) -------------------------
#
# A Textual app owns a terminal, so the three regions are three processes in three tmux
# panes. `ensure` is what builds that window, and it has to be idempotent against a console
# that already has it — a second `remote-agents` in a second terminal calls `ensure` too, and
# a fourth pane appearing because someone opened a second client would be a defect nobody
# would attribute to this method.


async def test_ensure_builds_three_panes_in_the_declared_proportions() -> None:
    console = RecordingConsole(exists=False)
    await _composer(console).ensure()

    assert named(console, "create_console") == [
        ("create_console", _PANE_COMMANDS[ConsolePaneSlot.PROJECTS], Path("/tmp"))
    ]
    splits = named(console, "split_console_pane")
    assert [(call[2], call[4], call[5]) for call in splits] == [
        (_PANE_COMMANDS[ConsolePaneSlot.SESSIONS], False, 40),
        (_PANE_COMMANDS[ConsolePaneSlot.FEED], True, 33),
    ]
    # The feed splits off the *sessions* pane, not the projects pane: splitting the left one
    # again would put the feed under projects and leave the sessions pane full height.
    assert splits[0][1] == "%0", "the sessions pane splits off the left slot"
    assert splits[1][1] == splits[0][6], "the feed splits off the pane sessions just made"


async def test_ensure_marks_every_pane_it_builds_with_the_slot_it_is() -> None:
    """Found by what it *is*, never by where it sits — DEC-040's rule for the surface,
    applied to all three. Position cannot say which pane is missing once one is gone."""
    console = RecordingConsole(exists=False)
    await _composer(console).ensure()

    assert [call[2] for call in named(console, "mark_console_slot")] == [
        ConsolePaneSlot.PROJECTS,
        ConsolePaneSlot.SESSIONS,
        ConsolePaneSlot.FEED,
    ]


async def test_re_ensure_adds_no_fourth_pane() -> None:
    console = RecordingConsole(arrangement=_three_pane_console())
    await _composer(console).ensure()

    assert named(console, "create_console") == []
    assert named(console, "split_console_pane") == []


async def test_a_console_missing_one_pane_regains_exactly_that_one() -> None:
    """Its process died, or a displayed agent's pane was killed with the console around it."""
    without_feed = tuple(
        pane for pane in _three_pane_console() if pane.console_slot != ConsolePaneSlot.FEED.value
    )
    console = RecordingConsole(arrangement=without_feed)
    await _composer(console).ensure()

    splits = named(console, "split_console_pane")
    assert len(splits) == 1, "exactly the missing one"
    assert splits[0][2] == _PANE_COMMANDS[ConsolePaneSlot.FEED]
    assert splits[0][1] == "%1", "and off the pane it belongs beside"


async def test_a_displayed_agent_does_not_make_the_console_build_a_second_surface() -> None:
    """The projects surface is not missing while an agent is displayed — it is *parked*.

    An exchange puts the agent's pane in the console's left slot and sends the surface to
    live in that agent's own window, carrying its mark with it. A check that looked only at
    the console would see no pane claiming the projects slot and split a second surface in
    beside the agent, leaving the owner with two and the original still parked elsewhere.
    """
    projects = ConsolePaneSlot.PROJECTS.value
    displayed = tuple(
        pane for pane in _three_pane_console() if pane.console_slot != projects
    ) + (
        HostedPane(
            host=None,
            on_console=True,
            window_index=0,
            pane_index=0,
            pane_id="%9",
            session_id=_RUNNING,
        ),
        # The surface, parked in the displayed agent's own window, still marked.
        HostedPane(
            host=_RUNNING,
            on_console=False,
            window_index=0,
            pane_index=0,
            pane_id="%0",
            session_id=None,
            surface=True,
            console_slot=projects,
        ),
    )
    console = RecordingConsole(arrangement=displayed)
    await _composer(console).ensure()

    assert named(console, "split_console_pane") == []
    assert named(console, "mark_console_slot") == []


async def test_a_dead_projects_pane_is_rebuilt_rather_than_stolen_from_its_neighbour() -> None:
    """The Critical a Tier-1 review found, pinned.

    When the projects pane is the one that dies, the leftmost survivor is the *sessions*
    pane. Reading it as "the pane the window was created with, simply unmarked" re-marked a
    live sessions pane as the projects surface — losing the surface permanently, with no
    exception and no log line, and leaving a pane labelled `surface` still running the
    sessions program. Only an unmarked pane may be adopted; a marked one is rebuilt beside.
    """
    projects = ConsolePaneSlot.PROJECTS.value
    survivors = tuple(pane for pane in _three_pane_console() if pane.console_slot != projects)
    console = RecordingConsole(arrangement=survivors)
    await _composer(console).ensure()

    marked = named(console, "mark_console_slot")
    assert [call[1] for call in marked] != ["%1"], "the sessions pane must keep its own mark"
    splits = named(console, "split_console_pane")
    assert len(splits) == 1
    assert splits[0][2] == _PANE_COMMANDS[ConsolePaneSlot.PROJECTS]
    assert splits[0][1] == "%1", "split off the sessions pane, the only one left to split from"
    assert splits[0][7] is True, "and *before* it, or the surface lands on the wrong side"
    assert [call[2] for call in marked] == [ConsolePaneSlot.PROJECTS]


async def test_a_console_reduced_to_a_displayed_agent_is_reported_rather_than_rebuilt() -> None:
    """Both right panes gone and the surface parked: nothing console-side to split from.

    The parked surface is a valid parent by mark and a terrible one in fact — splitting off
    it would put a console pane inside the agent's own session.
    """
    console = RecordingConsole(
        arrangement=(
            HostedPane(None, True, 0, 0, "%9", _RUNNING),
            HostedPane(
                _RUNNING, False, 0, 0, "%0", None, True, ConsolePaneSlot.PROJECTS.value
            ),
        )
    )
    await _composer(console).ensure()

    assert named(console, "split_console_pane") == []


# --- The tab mechanism is retired (Sub-plan 3, Task 2.4) ------------------------------


async def test_open_exchanges_the_left_pane_and_never_links_a_window() -> None:
    """`open` *is* `show` now: one meaning for "the console shows this session".

    Under the tab model this linked the session's window into the console and selected it,
    falling back to switching the client. All three are gone with the mechanism — a tab makes
    tmux list a linked window's panes twice, and both readers of that listing were caught
    disagreeing about where a pane was.
    """
    agent = HostedPane(None, False, 0, 0, "%7", _RUNNING)
    console = RecordingConsole(arrangement=(*_three_pane_console(), agent))
    await _composer(console).open(_RUNNING)

    assert [call[1:] for call in named(console, "swap_panes")] == [("%7", "%0")]


async def test_the_composer_has_no_tab_operations_left_to_call() -> None:
    """The retirement, asserted against the composer rather than against a grep.

    A method that survives with no caller is how a mechanism comes back: the next author
    finds it, assumes it is supported, and wires it up.
    """
    from remote_agents.application.console import ConsoleComposer

    retired = {
        "link_session_window",
        "unlink_console_window",
        "select_console_window",
        "switch_client_to_session",
        "console_windows",
        "console_active_window",
    }
    source = Path(ConsoleComposer.__module__.replace(".", "/") + ".py")
    text = (Path("src") / source).read_text(encoding="utf-8")
    for name in retired:
        assert f"self._console.{name}" not in text, f"the composer still calls {name}"
    # And the port no longer declares them, so this double could not grow them back either.
    from remote_agents.ports.console import ConsolePort

    assert retired.isdisjoint(vars(ConsolePort)), "the port still declares a retired operation"


async def test_a_rebuild_puts_the_window_back_in_its_declared_proportions() -> None:
    """A rebuilt pane inherits the shape of what it was split from, not its own.

    Measured on real tmux at the Stage 2 gate: kill the projects pane and the one that
    replaces it is a 48x16 box in the top-left with the feed running the **full width**
    beneath both — correct marks, correct side, wrong window. `-b` chooses a side; it cannot
    undo a layout tree that changed while the pane was missing.
    """
    projects = ConsolePaneSlot.PROJECTS.value
    survivors = tuple(pane for pane in _three_pane_console() if pane.console_slot != projects)
    console = RecordingConsole(arrangement=survivors)
    await _composer(console).ensure()

    assert named(console, "normalize_console_layout") == [
        ("normalize_console_layout", 60, "%2", 33)
    ]


async def test_a_console_already_at_rest_is_never_resized_underneath_the_owner() -> None:
    """`ensure` runs every time a second terminal opens the console. Normalizing there would
    undo any resize the owner made on purpose, which is worse than the defect above."""
    console = RecordingConsole(arrangement=_three_pane_console())
    await _composer(console).ensure()

    assert named(console, "normalize_console_layout") == []


async def test_a_duplicated_pane_slot_is_reported_rather_than_left_silent() -> None:
    """Two panes claiming the same slot is reachable, and nothing here removes one.

    The composer's lock is per-process and every pane surface calls `ensure` at start, so two
    overlapping callers reading the same stale arrangement can each split for the same
    missing slot. The final gate's evaluator found a console with five panes in it that way.
    Repair would mean killing a pane this composer cannot be sure it created, so it says so
    instead — on the surface, where `settle`'s blocked notes are rendered.
    """
    doubled = (
        *_three_pane_console(),
        HostedPane(
            host=None,
            on_console=True,
            window_index=0,
            pane_index=3,
            pane_id="%7",
            session_id=None,
            console_slot=ConsolePaneSlot.SESSIONS.value,
        ),
    )
    console = RecordingConsole(arrangement=doubled)
    report = await _composer(console).settle()

    assert any("more than one sessions pane" in note for note in report.blocked), report.blocked
    assert named(console, "split_console_pane") == [], "reporting is not repairing"


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


async def test_the_binding_budget_is_one_key_and_it_says_why() -> None:
    """A second root binding should have to be argued for here, not appear silently.

    The plan allowed the projects key plus *at most* two for pane focus. What the layout
    actually needs is one. A focus key was declared and then removed at the Stage 2 gate: its
    argument was that a displayed agent consumes the prefix key, which is false — tmux
    intercepts the prefix in the client, so `prefix + o` already cycles the three panes at no
    cost to any agent. A key that buys one keystroke over an existing chord does not earn a
    permanent claim on every agent's keyboard.
    """
    assert len(CONSOLE_BINDINGS) == 1
    assert len({binding.key for binding in CONSOLE_BINDINGS}) == 1
    for binding in CONSOLE_BINDINGS:
        assert binding.why.strip(), f"{binding.key} is spent forever and does not say why"


async def test_the_projects_binding_carries_our_own_command() -> None:
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
    """A missing key costs the owner a key, never a console.

    This test's name said so while its assertion said `ensure() is False` — and `False` is
    what `_enter_console` reads as "the console could not be prepared", so it refused to
    attach to a console whose three panes were built and running. Found by the Stage 2 gate
    evaluator, which noticed the name and the assertion contradicting each other.
    """
    console = RecordingConsole(arrangement=_three_pane_console())

    async def refuse(key, action, command=()):
        raise RuntimeError("tmux refused the bind")

    console.install_console_binding = refuse  # type: ignore[method-assign]
    assert await _composer(console).ensure() is True


async def test_a_console_that_cannot_be_built_is_still_a_failure() -> None:
    """The other side of the line above: panes are the console, keys are a convenience."""
    console = RecordingConsole(exists=False)

    async def refuse(command, cwd):
        raise RuntimeError("tmux refused the new-session")

    console.create_console = refuse  # type: ignore[method-assign]
    assert await _composer(console).ensure() is False





async def test_a_raising_console_degrades_to_nothing_and_raises_into_no_caller() -> None:
    broken = RecordingConsole(error=RuntimeError("tmux exploded"))
    composer = _composer(broken)
    assert await composer.ensure() is False
    await composer.sync((_record(_RUNNING, SessionState.RUNNING),))  # must not raise



async def test_flash_is_suppressed_while_the_feed_that_carries_it_is_on_screen() -> None:
    """Do not say one thing twice on one screen — the same rule, on a premise that still holds.

    It used to ask whether the console's current window was 0, meaning the client rested on
    the dashboard tab. With the tabs retired the console has exactly one window, so that
    question answers itself and the flash could never have fired again. Under three panes the
    feed is beside whatever the owner is doing, so the only arrangement that hides it is a
    zoomed pane — where tmux still draws the status bar, which is exactly when a one-line
    nudge earns its place.
    """
    console = RecordingConsole(arrangement=_three_pane_console())
    console.zoomed_pane = None
    await _composer(console).flash("the agent is waiting for an answer")
    assert named(console, "display_message") == [], "the feed is visible; it already says this"

    console.zoomed_pane = "%2"  # the feed itself, zoomed
    await _composer(console).flash("the agent is waiting for an answer")
    assert named(console, "display_message") == [], "still the feed on screen, larger"

    console.zoomed_pane = "%0"  # an agent, or the projects surface, zoomed over the feed
    await _composer(console).flash("the agent is waiting for an answer")
    assert named(console, "display_message") == [
        ("display_message", "the agent is waiting for an answer")
    ]


async def test_a_failing_flash_is_silence_never_an_exception() -> None:
    broken = RecordingConsole(error=RuntimeError("no server"))
    await _composer(broken).flash("news")  # must not raise
