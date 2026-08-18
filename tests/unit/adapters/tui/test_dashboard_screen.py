"""The resting position is a dashboard: projects left, sessions and feed on the right.

The dashboard subclasses the projects picker rather than replacing it, so every behavior
the resting position already had — the filter and its debounce, the catalogue refresh,
the back-path guarantee of a stack that can never empty — is inherited, not re-implemented.
What this file pins is the new shape: three panes exist, focus can reach both lists, each
pane declares its empty state (DEC-009), the app rests on the dashboard, and a pushed flow
still returns to it on Escape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from textual.widgets import OptionList, Static

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.screens.dashboard import DashboardScreen
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

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
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
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


def _context(records: tuple[SessionRecord, ...] = ()) -> TuiContext:
    return TuiContext(
        launcher=_Launcher(records),  # type: ignore[arg-type]
        creator=_Creator(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


def test_the_resting_screen_is_the_dashboard() -> None:
    assert isinstance(RemoteAgentsTui(_context()).get_default_screen(), DashboardScreen)


async def test_three_panes_render_and_the_session_appears_in_its_pane() -> None:
    app = RemoteAgentsTui(_context((_record(),)))
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        projects = screen.query_one("#choices", OptionList)
        sessions = screen.query_one("#sessions-pane", OptionList)
        feed = screen.query_one("#feed-pane", Static)
        assert projects.option_count >= 1
        assert sessions.option_count == 1
        assert feed.display is True


async def test_the_sessions_pane_declares_its_empty_state() -> None:
    app = RemoteAgentsTui(_context(()))
    async with app.run_test() as pilot:
        await pilot.pause()
        sessions = app.screen.query_one("#sessions-pane", OptionList)
        assert sessions.option_count == 1
        option = sessions.get_option_at_index(0)
        assert option.disabled is True
        assert "No sessions" in str(option.prompt)


async def test_focus_can_reach_both_lists() -> None:
    app = RemoteAgentsTui(_context((_record(),)))
    async with app.run_test() as pilot:
        await pilot.pause()
        sessions = app.screen.query_one("#sessions-pane", OptionList)
        sessions.focus()
        await pilot.pause()
        assert app.focused is sessions
        projects = app.screen.query_one("#choices", OptionList)
        projects.focus()
        await pilot.pause()
        assert app.focused is projects


async def test_a_pushed_flow_returns_to_the_dashboard_on_escape() -> None:
    app = RemoteAgentsTui(_context())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        await app.push_screen(
            __import__(
                "remote_agents.adapters.tui.screens.launch", fromlist=["ProfilesScreen"]
            ).ProfilesScreen()
        )
        await pilot.pause()
        assert not isinstance(app.screen, DashboardScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
