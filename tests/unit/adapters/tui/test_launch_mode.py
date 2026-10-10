"""The TUI offers "Shared folder / Own worktree" only on contention (BL-116).

Pilot-driven through the launch flow: with no other session live on the project, choosing an
agent launches as it always has; with one, a choice screen comes first, and Escape from it
launches nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from backends import tui_context_for
from tui_feedback import announcements, status
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.services import LaunchOutcome
from remote_agents.domain.models import SessionId, SessionState

_PROJECT = CatalogProject("opaque-existing", "existing", "dev-area", "Registered")


@dataclass(slots=True)
class _Record:
    orphan_provenance = None

    session_id: SessionId
    state: SessionState


class _Launcher:
    def __init__(
        self,
        *,
        offered: bool,
        state: SessionState = SessionState.RUNNING,
        failed: str | None = None,
    ) -> None:
        self.offered = offered
        self.state = state
        self.failed = failed
        self.commands: list[LaunchCommand] = []

    async def project_usage(self) -> tuple[()]:
        return ()

    async def worktree_offered(self, project_id) -> bool:
        return self.offered

    async def launch(self, command: LaunchCommand) -> LaunchOutcome:
        self.commands.append(command)
        return LaunchOutcome(
            _Record(SessionId.new(), self.state),  # type: ignore[arg-type]
            False,
            worktree_failed=self.failed,
        )


class _NoCreation:
    def available_areas(self) -> tuple[str, ...]:
        return ()


def _app(launcher: _Launcher) -> RemoteAgentsTui:
    return RemoteAgentsTui(
        tui_context_for(
            sessions=launcher,
            projects=_NoCreation(),
            profiles=(ProfileAvailability("claude", True),),
            refresh_catalogue=lambda: (_PROJECT,),
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
            catalogue=(_PROJECT,),
        )
    )


async def _choose(app: RemoteAgentsTui, pilot, key: str) -> None:
    await app.screen.choose(key)
    await pilot.pause()


async def _to_profiles(app: RemoteAgentsTui, pilot) -> None:
    await _choose(app, pilot, "opaque-existing")
    await _choose(app, pilot, "launch")
    assert position(app) == "PROFILES"


async def test_with_no_contention_enter_on_a_profile_launches_regular() -> None:
    launcher = _Launcher(offered=False)
    app = _app(launcher)
    async with app.run_test() as pilot:
        await _to_profiles(app, pilot)
        await _choose(app, pilot, "claude")

    assert [command.mode for command in launcher.commands] == ["regular"]


async def test_with_contention_a_choice_comes_first_and_own_worktree_launches_one() -> None:
    launcher = _Launcher(offered=True)
    app = _app(launcher)
    async with app.run_test() as pilot:
        await _to_profiles(app, pilot)
        await _choose(app, pilot, "claude")
        assert position(app) == "MODE"
        assert launcher.commands == []
        assert [str(option.prompt) for option in app.screen.query("#choices").first().options][
            :2
        ] == ["Shared folder", "Own worktree"]
        await _choose(app, pilot, "worktree")

    assert [command.mode for command in launcher.commands] == ["worktree"]
    assert str(launcher.commands[0].profile_id) == "claude"


async def test_shared_folder_launches_regular() -> None:
    launcher = _Launcher(offered=True)
    app = _app(launcher)
    async with app.run_test() as pilot:
        await _to_profiles(app, pilot)
        await _choose(app, pilot, "claude")
        await _choose(app, pilot, "regular")

    assert [command.mode for command in launcher.commands] == ["regular"]


async def test_escape_from_the_choice_returns_to_the_agents_and_launches_nothing() -> None:
    launcher = _Launcher(offered=True)
    app = _app(launcher)
    async with app.run_test() as pilot:
        await _to_profiles(app, pilot)
        await _choose(app, pilot, "claude")
        assert position(app) == "MODE"
        await pilot.press("escape")
        await pilot.pause()
        assert position(app) == "PROFILES"

    assert launcher.commands == []


async def test_a_worktree_that_could_not_be_made_is_reported_and_nothing_attaches() -> None:
    launcher = _Launcher(offered=True, state=SessionState.FAILED, failed="fatal: bad ref")
    app = _app(launcher)
    async with app.run_test() as pilot:
        await _to_profiles(app, pilot)
        await _choose(app, pilot, "claude")
        await _choose(app, pilot, "worktree")
        assert position(app) == "MODE"
        line, toasts = status(app), announcements(app)

    assert len(launcher.commands) == 1
    assert "Nothing was started" in line
    assert any("Its worktree could not be made: fatal: bad ref" in toast for toast in toasts)
