"""Choosing a project asks one question — launch new, or reopen saved — then delegates.

The chooser is navigation over flows both surfaces already have, not a new wizard step
(DEC-033): Launch lands on the existing agent picker with a fresh selection, Resume lands
on the existing resume-capable agent list, and a host with no conversations service simply
never shows Resume (DEC-009's no-third-answer rule — a dead-end entry is worse than an
absent one).
"""

from __future__ import annotations

from pathlib import Path

from backends import backend_for
from textual.widgets import OptionList

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.screens.dashboard import DashboardScreen, ProjectChooserScreen
from remote_agents.adapters.tui.screens.launch import ProfilesScreen
from remote_agents.adapters.tui.screens.resume import ResumeProfilesScreen
from remote_agents.application.project_admin import CreatedProject, CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.conversations import ProfileResumeCapability
from remote_agents.domain.models import ProfileId
from remote_agents.domain.projects import ProjectIdentity

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")


class _Creator:
    def available_areas(self) -> tuple[str, ...]:
        return ("dev-area", "infra")

    def create(self, command: CreateProjectCommand) -> CreatedProject:
        identity = ProjectIdentity(area=command.area, name=command.name)
        return CreatedProject(identity, Path("/dev") / command.area / command.name)


class _Launcher:
    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self) -> tuple:
        return ()


class _Conversations:
    async def capabilities(self) -> tuple[ProfileResumeCapability, ...]:
        return (ProfileResumeCapability(ProfileId("claude"), True, True, None),)


def _context(*, conversations: object | None = None) -> TuiContext:
    return TuiContext(
        backend=backend_for(
            sessions=_Launcher(),  # type: ignore[arg-type]
            projects=_Creator(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_PROJECT,),
            catalogue=(_PROJECT,),
            conversations=conversations,  # type: ignore[arg-type]
        ),
        profiles=(ProfileChoice("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


def _keys(screen: ProjectChooserScreen) -> list[str | None]:
    choices = screen.query_one("#choices", OptionList)
    return [choices.get_option_at_index(i).id for i in range(choices.option_count)]


async def test_choosing_a_project_opens_the_chooser() -> None:
    app = RemoteAgentsTui(_context(conversations=_Conversations()))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        assert isinstance(app.screen, ProjectChooserScreen)


async def test_both_entries_show_only_when_resume_is_possible_here() -> None:
    with_resume = RemoteAgentsTui(_context(conversations=_Conversations()))
    async with with_resume.run_test() as pilot:
        await with_resume.screen.choose("opaque-existing")
        await pilot.pause()
        assert _keys(with_resume.screen)[:2] == ["launch", "resume"]

    without = RemoteAgentsTui(_context(conversations=None))
    async with without.run_test() as pilot:
        await without.screen.choose("opaque-existing")
        await pilot.pause()
        keys = _keys(without.screen)
        assert "launch" in keys and "resume" not in keys


async def test_launch_lands_on_the_agent_picker_with_a_fresh_selection() -> None:
    app = RemoteAgentsTui(_context(conversations=_Conversations()))
    async with app.run_test() as pilot:
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("launch")
        await pilot.pause()
        assert isinstance(app.screen, ProfilesScreen)
        assert app.selection.project == _PROJECT
        assert app.selection.profile is None


async def test_resume_lands_on_the_resume_capable_agent_list() -> None:
    app = RemoteAgentsTui(_context(conversations=_Conversations()))
    async with app.run_test() as pilot:
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("resume")
        await pilot.pause()
        assert isinstance(app.screen, ResumeProfilesScreen)
        assert app.screen.project == _PROJECT
