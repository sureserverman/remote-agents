"""Ctrl+S reaches the sessions view from anywhere the wizard can be, and nowhere unsafe."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pytest
from backends import SessionUseCaseDouble, backend_for
from console_selection import SelectionConsole
from textual.widgets import OptionList
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.panes import ProjectsPane
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_EXISTING = CatalogProject("opaque-existing", "existing", "infra", "Registered")


def _record() -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _Listing(SessionUseCaseDouble):
    records: tuple[SessionRecord, ...] = ()
    refreshed: int = 0

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        self.refreshed += 1
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        return None


class _Creator:
    def available_areas(self) -> tuple[str, ...]:
        return ("infra",)


def _context(launcher: _Listing) -> TuiContext:
    return TuiContext(
        backend=backend_for(
            sessions=launcher,  # type: ignore[arg-type]
            projects=_Creator(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_EXISTING,),
            catalogue=(_EXISTING,),
        ),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


def test_ctrl_s_is_bound_and_shown_in_the_footer() -> None:
    bindings = {binding.key: binding for binding in RemoteAgentsTui.BINDINGS}
    assert "ctrl+s" in bindings
    assert bindings["ctrl+s"].action == "sessions"
    assert bindings["ctrl+s"].description


def test_the_existing_bindings_keep_their_behavior() -> None:
    """Adding a binding must not renumber or rebind what the owner already knows."""
    bindings = {binding.key: binding.action for binding in RemoteAgentsTui.BINDINGS}
    assert bindings["escape"] == "back"
    assert bindings["ctrl+r"] == "refresh"
    assert bindings["ctrl+n"] == "add_project"
    assert bindings["ctrl+q"] == "quit"


@pytest.mark.parametrize(
    "step_setup",
    ["projects", "chooser", "profiles", "areas"],
)
async def test_ctrl_s_opens_sessions_from_any_wizard_step(step_setup: str) -> None:
    """Ctrl+S reaches the sessions list from every position the launch wizard has.

    **Two of these cases were not reaching the positions they were named for, and the test was
    green throughout.** `"profiles"` walked one `choose` and stopped — which reached the agent
    list when it was written, and stopped doing so the day the Launch-or-Resume chooser was
    inserted between them (DEC-033). `"review"` walked a second `choose("claude")` on top,
    which is not a row the chooser offers, so it did nothing at all: both cases sat on
    `PROJECT_CHOOSER`, testing it twice under two wrong names while the agent list — the
    position with the most bindings of the three — went untested.

    Neither was caused by removing the review position; the collapse predates it, and removing
    the review is what made it visible, since `"review"` no longer names anything. Fixed by
    walking each case to the position it claims **and asserting it arrived**: a parametrization
    whose cases silently converge is one that reports four times the coverage it has, and the
    assertion is the only part of this that a future insertion cannot quietly undo.
    """
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))
    expected = {
        "projects": "DASHBOARD",
        "chooser": "PROJECT_CHOOSER",
        "profiles": "PROFILES",
        "areas": "AREAS",
    }[step_setup]

    async with app.run_test() as pilot:
        if step_setup in {"chooser", "profiles"}:
            await app.screen.choose("opaque-existing")
            await pilot.pause()
        if step_setup == "profiles":
            await app.screen.choose("launch")
            await pilot.pause()
        elif step_setup == "areas":
            await app.show_areas()
        await pilot.pause()
        assert position(app) == expected, (
            f"the {step_setup!r} setup reached {position(app)}, not {expected} — this case is "
            f"not testing the step it is named for"
        )

        await app.action_sessions()
        await pilot.pause()
        step = position(app)

    assert step == "SESSIONS"


async def test_ctrl_s_is_refused_while_busy() -> None:
    """Matching the existing guard on refresh and add-project."""
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await pilot.pause()
        # The dashboard's own mount reload is the baseline; the refusal is about the key
        # adding nothing on top of it.
        before = launcher.refreshed
        app._busy = True
        await app.action_sessions()
        await pilot.pause()
        step = position(app)

    assert step == "DASHBOARD"
    assert launcher.refreshed == before


async def test_pressing_the_key_actually_reaches_the_action() -> None:
    """Binding tables can be right while the keystroke still goes nowhere."""
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await pilot.pause()
        before = launcher.refreshed
        await pilot.press("ctrl+s")
        await pilot.pause()
        step = position(app)

    assert step == "SESSIONS"
    assert launcher.refreshed == before + 1


async def test_ctrl_r_on_the_sessions_list_re_lists_it_and_stays_put() -> None:
    """The one view whose answer goes stale on its own is the one Refresh used to abandon.

    A second process writes the same store, so the sessions list is the position where the
    owner has an actual reason to press Refresh. It re-read the *catalogue* and unwound to
    the project picker instead, which is neither of the two things the key promises here.
    """
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert position(app) == "SESSIONS"
        listed, depth = launcher.refreshed, len(app.screen_stack)

        await pilot.press("ctrl+r")
        await pilot.pause()

        assert launcher.refreshed == listed + 1, "refresh did not re-run the sessions load"
        assert position(app) == "SESSIONS"
        assert len(app.screen_stack) == depth, "refresh moved the owner off the sessions list"


async def test_ctrl_r_where_there_is_nothing_to_re_read_does_not_navigate() -> None:
    """A screen with nothing to refresh stays where it is, rather than unwinding the stack.

    Task 1.2 turns this into a disabled binding the footer stops advertising; until then the
    property that matters is that the key cannot move the owner.
    """
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("launch")
        await pilot.pause()
        assert position(app) == "PROFILES"
        depth = len(app.screen_stack)

        await pilot.press("ctrl+r")
        await pilot.pause()

        assert position(app) == "PROFILES"
        assert len(app.screen_stack) == depth


def test_no_app_binding_is_swallowed_by_a_focusable_widget() -> None:
    """A binding a focused widget also claims never reaches the app.

    Ctrl+E shipped briefly as the Resume key. Textual's Input binds it to `end`, and the
    app starts with the filter focused, so pressing it on the opening screen did nothing —
    invisible to any test that called the action method directly.
    """
    from textual.widgets import Input, OptionList

    from remote_agents.adapters.tui.app import RemoteAgentsTui

    def keys_of(source) -> dict[str, str]:
        found: dict[str, str] = {}
        for binding in source.BINDINGS:
            key = getattr(binding, "key", str(binding))
            for part in str(key).split(","):
                found[part.strip()] = getattr(binding, "action", "?")
        return found

    app_keys = keys_of(RemoteAgentsTui)
    for widget in (Input, OptionList):
        clashes = {
            key: (app_keys[key], keys_of(widget)[key]) for key in app_keys if key in keys_of(widget)
        }
        assert not clashes, f"{widget.__name__} swallows {clashes}"


def test_every_screen_that_advertises_refresh_actually_implements_it() -> None:
    """`can_refresh` and `refresh_contents` are two declarations of one fact.

    The flag exists because the next task drives `check_action` off it, and asking "did this
    class replace a method" is not a question a binding check should be answering at runtime.
    That is a fair call, and it leaves the two free to disagree — with the footer taking the
    flag's word for it.

    Both directions are defects, and they are the *same* defect this task exists to fix, moved
    up a level: a screen that advertises Refresh without implementing it lies to the owner
    exactly as the old unconditional catalogue re-read did, and one that implements it without
    advertising it has working behaviour the next task will make unreachable.
    """
    from remote_agents.adapters.tui.screens import ALL_SCREENS
    from remote_agents.adapters.tui.screens.base import ChoiceScreen

    def implements(screen) -> bool:
        return screen.refresh_contents is not ChoiceScreen.refresh_contents

    disagreeing = {
        screen.__name__: (screen.can_refresh, implements(screen))
        for screen in ALL_SCREENS
        if issubclass(screen, ChoiceScreen) and screen.can_refresh != implements(screen)
    }
    assert not disagreeing, (
        "these screens declare `can_refresh` and override `refresh_contents` inconsistently "
        f"— (can_refresh, overrides) per screen: {disagreeing}"
    )


async def test_the_selection_capability_is_absent_off_a_console() -> None:
    """Declared absence, not a probe (DEC-046, DEC-061).

    A surface that is not hosted by a console has no published selection to read and nothing
    to publish to. Both capabilities are then `None`, and the Alt chord layer is not offered at
    all rather than offered and inert — a dead-end key is worse than an absent one, which is
    the same reasoning that gates `p` to the sessions pane.
    """
    context = _context(_Listing(()))

    assert context.console_publish_selection is None
    assert context.console_read_selection is None


async def test_the_selection_capability_publishes_and_reads_when_wired() -> None:
    """Two fields rather than one object, because no pane needs both.

    Publishing belongs to the one position that owns a cursor; reading belongs to every
    position that does not. Splitting them means a pane holding only the reader cannot
    accidentally become a second writer of a fact that must have exactly one.
    """
    console = SelectionConsole()
    chosen = SessionId.new()
    context = replace(
        _context(_Listing(())),
        console_publish_selection=console.publish,
        console_read_selection=console.read,
    )

    await context.console_publish_selection(chosen)
    await context.console_publish_selection(None)
    console.selected = chosen

    assert console.published == [chosen, None]
    assert await context.console_read_selection() == chosen


async def test_selected_session_on_a_sessions_position_answers_from_its_own_cursor() -> None:
    """A position with a cursor never asks the console what it already knows.

    Reading the published option here would answer with whatever the *pane* has highlighted,
    which on the full sessions position is a different process's cursor entirely — and on the
    pane itself would be a round trip to read back what it had just written.
    """
    console = SelectionConsole(selected=SessionId.new())
    records = (_record(), _record())
    app = RemoteAgentsTui(
        replace(
            _context(_Listing(records)),
            console_publish_selection=console.publish,
            console_read_selection=console.read,
        )
    )

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.highlighted = 1

        assert await app.selected_session() == choices.get_option_at_index(1).id
        assert console.reads == 0, "a position with its own cursor read the console anyway"


async def test_selected_session_elsewhere_answers_from_the_published_selection() -> None:
    """The projects pane has no sessions list, so the answer comes from the pane that does."""
    chosen = SessionId.new()
    console = SelectionConsole(selected=chosen)
    app = ProjectsPane(
        replace(
            _context(_Listing(())),
            console_publish_selection=console.publish,
            console_read_selection=console.read,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert not getattr(app.screen, "owns_session_cursor", False)

        assert await app.selected_session() == str(chosen)
        assert console.reads == 1

        # Never cached: the owner can move the cursor in the other pane between two presses,
        # and the second press must act on where it is now.
        moved = SessionId.new()
        console.selected = moved
        assert await app.selected_session() == str(moved)
        assert console.reads == 2, "the second press reused a cached selection"


async def test_selected_session_is_nothing_off_a_console() -> None:
    """No cursor here and no console to ask, so there is nothing for a chord to act on.

    The chord layer is not offered at all in this case (Task 3.1), but this is the value it is
    gated on, and "nothing" has to be a returned answer rather than a raised one.
    """
    app = ProjectsPane(_context(_Listing(())))

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert app.services.console_read_selection is None
        assert await app.selected_session() is None
