"""The left pane: the projects surface on its own, without the two right-hand regions.

The combined dashboard put projects, sessions and the feed in one Textual app. Under the
three-pane console the left pane is its own process, and what it must still be is the whole
projects position — the catalogue, the filter and its debounce, the Launch/Resume chooser,
and the flows those push. So this pane *subclasses* the projects picker and the dashboard
subclasses the pane, which is what keeps one implementation of "choosing a project opens the
chooser" rather than two that drift.

What is pinned here is the pane's own shape: it rests on the projects position, it has no
sessions or feed region, the filter narrows, the chooser opens, Launch reaches the agent
list, its empty state is declared (DEC-009), and the cursor rests drawn on a non-mutating
row with the keyboard in the filter, which is what `test_resting_cursor.py` requires of every
list this surface shows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.widgets import Input, OptionList
from tui_positions import position

from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.panes import ProjectsPane
from remote_agents.adapters.tui.screens.dashboard import (
    ProjectChooserScreen,
    ProjectsPaneScreen,
)
from remote_agents.application.project_admin import CreatedProject, CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.projects import ProjectIdentity

_INFRA = CatalogProject("opaque-infra", "remote-agents", "infra", "Registered")
_TOOLS = CatalogProject("opaque-tools", "opaque-shift", "dev-area", "Registered")
_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")


class _Creator:
    def available_areas(self) -> tuple[str, ...]:
        return ("dev-area", "infra")

    def create(self, command: CreateProjectCommand) -> CreatedProject:
        identity = ProjectIdentity(area=command.area, name=command.name)
        return CreatedProject(identity, Path("/dev") / command.area / command.name)


class _Launcher:
    def __init__(self, records: tuple[SessionRecord, ...] = ()) -> None:
        self.records = records

    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records


def _record() -> SessionRecord:
    return SessionRecord(
        _SESSION,
        ProjectId("opaque-infra"),
        ProfileId("claude"),
        SessionDisplayIdentity("remote-agents", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


def _context(**overrides) -> TuiContext:
    base = {
        "launcher": _Launcher((_record(),)),
        "creator": _Creator(),
        "profiles": (ProfileChoice("claude", True),),
        "refresh_catalogue": lambda: (_INFRA, _TOOLS),
        "attach_argv": lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        "catalogue": (_INFRA, _TOOLS),
    }
    base.update(overrides)
    return TuiContext(**base)  # type: ignore[arg-type]


def test_the_projects_pane_rests_on_the_projects_position() -> None:
    assert isinstance(ProjectsPane(_context()).get_default_screen(), ProjectsPaneScreen)


async def test_the_pane_has_neither_right_hand_region() -> None:
    """The two regions are other processes now; a copy of them here would be a second one."""
    app = ProjectsPane(_context())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert position(app) == "PROJECTS"
        assert not app.screen.query("#sessions-pane")
        assert not app.screen.query("#feed-pane")


async def test_the_filter_narrows_the_catalogue() -> None:
    app = ProjectsPane(_context())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert choices.option_count == 2
        app.screen.render_projects("opaque-shift")
        await pilot.pause()
        assert [option.id for option in choices.options] == ["opaque-tools"]


async def test_choosing_a_project_opens_the_chooser() -> None:
    """Not straight into the agent list: Launch and Resume are one question per project."""
    app = ProjectsPane(_context())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await app.screen.choose("opaque-infra")
        await pilot.pause()
        assert isinstance(app.screen, ProjectChooserScreen)
        assert position(app) == "PROJECT_CHOOSER"


async def test_launch_from_the_chooser_reaches_the_agent_list() -> None:
    app = ProjectsPane(_context())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await app.screen.choose("opaque-infra")
        await pilot.pause()
        await app.screen.choose("launch")
        await pilot.pause()
        assert position(app) == "PROFILES"
        assert app.selection.project == _INFRA


async def test_the_pane_declares_its_empty_state() -> None:
    """DEC-009: a filter that matches nothing says so rather than showing a blank list."""
    app = ProjectsPane(_context())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen.render_projects("nothing-matches-this")
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert choices.option_count == 1
        option = choices.get_option_at_index(0)
        assert option.disabled is True
        assert "No project matches that filter." in str(option.prompt)


async def test_the_keyboard_rests_in_the_filter_and_one_down_draws_the_cursor() -> None:
    """The resting-cursor discipline as this position actually holds it.

    The projects list renders with `focus=False`, so it deliberately draws **no** cursor while
    it is resting: the keyboard is in the filter, where typing narrows instead of being
    swallowed, and a highlighted row under a keyboard that is somewhere else would advertise
    an enter target the owner cannot reach. The discipline that does bind here is the one
    BL-004 states for the sessions pane — **one** key from the resting position reaches the
    first row — and from there the cursor must be *painted*, which is the half that was
    missing in the defect `test_resting_cursor.py` was written for. So this asserts the
    rendered highlight rather than the index, exactly as that file insists.
    """
    app = ProjectsPane(_context())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert isinstance(app.focused, Input)
        assert app.focused.id == "filter"
        assert choices.highlighted is None, "a resting list must not advertise an enter target"

        await pilot.press("down")
        await pilot.pause()
        assert app.focused is choices
        assert choices.highlighted == 0
        cursor = choices.get_visual_style(
            "option-list--option", "option-list--option-highlighted"
        ).rich_style.clear_meta_and_links()
        painted = [
            segment
            for line in range(choices.scrollable_content_region.height)
            for segment in choices.render_line(line)
            if segment.text.strip() and segment.style is not None
        ]
        assert any(
            segment.style.clear_meta_and_links() == cursor for segment in painted
        ), "the projects pane drew no cursor on the row the keyboard had reached"


@pytest.mark.parametrize("nothing", [()])
async def test_an_empty_catalogue_still_rests_somewhere(nothing) -> None:
    app = ProjectsPane(_context(refresh_catalogue=lambda: nothing, catalogue=nothing))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert position(app) == "PROJECTS"
        assert app.is_running
