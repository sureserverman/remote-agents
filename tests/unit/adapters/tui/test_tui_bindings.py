"""Ctrl+S reaches the sessions view from anywhere the wizard can be, and nowhere unsafe."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
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
class _Listing:
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
        launcher=launcher,  # type: ignore[arg-type]
        creator=_Creator(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_EXISTING,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_EXISTING,),
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
    ["projects", "profiles", "review", "areas"],
)
async def test_ctrl_s_opens_sessions_from_any_wizard_step(step_setup: str) -> None:
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        if step_setup == "profiles":
            await app.screen.choose("opaque-existing")
        elif step_setup == "review":
            await app.screen.choose("opaque-existing")
            await app.screen.choose("claude")
        elif step_setup == "areas":
            await app.show_areas()
        await pilot.pause()

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
