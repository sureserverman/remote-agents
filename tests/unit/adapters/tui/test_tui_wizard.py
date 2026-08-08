"""Unit tests for the local terminal wizard, driven headlessly through Textual."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from textual.widgets import OptionList

from remote_agents.adapters.tui.app import (
    _BACK,
    _CANCEL,
    AttachRequest,
    RemoteAgentsTui,
    label_or_error,
)
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.errors import ProjectCreationError
from remote_agents.application.project_admin import CreatedProject, CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import SessionId, SessionState
from remote_agents.domain.projects import ProjectIdentity

_EXISTING = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_OTHER = CatalogProject("opaque-other", "other-thing", "dev-area", "Unregistered")


@dataclass(slots=True)
class FakeRecord:
    session_id: SessionId
    state: SessionState


class FakeLauncher:
    """Accept one launch and report the state the test asked for."""

    def __init__(self, state: SessionState = SessionState.RUNNING) -> None:
        self.state = state
        self.commands: list[LaunchCommand] = []

    async def launch(self, command: LaunchCommand) -> FakeRecord:
        self.commands.append(command)
        return FakeRecord(SessionId.new(), self.state)


class FakeCreator:
    def __init__(
        self, areas: tuple[str, ...] = ("dev-area", "infra"), error: Exception | None = None
    ) -> None:
        self._areas = areas
        self.error = error
        self.commands: list[CreateProjectCommand] = []

    def available_areas(self) -> tuple[str, ...]:
        return self._areas

    def create(self, command: CreateProjectCommand) -> CreatedProject:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        identity = ProjectIdentity(area=command.area, name=command.name)
        return CreatedProject(identity, Path("/dev") / command.area / command.name)


def _context(**overrides: object) -> TuiContext:
    arguments: dict[str, object] = {
        "launcher": FakeLauncher(),
        "creator": FakeCreator(),
        "profiles": (
            ProfileChoice("claude", True),
            ProfileChoice("cursor-agent", False, "executable_missing"),
        ),
        "refresh_catalogue": lambda: (_EXISTING, _OTHER),
        "attach_argv": lambda session_id: (
            "tmux",
            "-L",
            "remote-agents",
            "attach-session",
            "-t",
            f"={session_id}",
        ),
        "catalogue": (_EXISTING, _OTHER),
    }
    arguments.update(overrides)
    return TuiContext(**arguments)  # type: ignore[arg-type]


def _rows(app: RemoteAgentsTui) -> list[str]:
    return [str(option.prompt) for option in app.query_one("#choices", OptionList).options]


def _keys(app: RemoteAgentsTui) -> list[str]:
    return [option.id for option in app.query_one("#choices", OptionList).options]


def _status(app: RemoteAgentsTui) -> str:
    return str(app.query_one("#status").content)


async def test_the_project_list_shows_registered_before_unregistered_with_its_group() -> None:
    app = RemoteAgentsTui(_context())

    async with app.run_test():
        rows = _rows(app)

    assert rows == ["infra/existing  [Registered]", "dev-area/other-thing  [Unregistered]"]


async def test_typing_filters_the_project_list_one_character_at_a_time() -> None:
    """A refill must not steal the keyboard, or every character after the first is lost."""
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        for character in "other":
            await pilot.press(character)
        await pilot.pause()
        typed = app.query_one("#filter").value
        rows = _rows(app)

    assert typed == "other"
    assert rows == ["dev-area/other-thing  [Unregistered]"]


async def test_the_agent_list_names_every_curated_profile_with_its_blocking_reason() -> None:
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        app._choose_project("opaque-existing")
        await pilot.pause()
        rows = _rows(app)

    assert rows == ["claude", "cursor-agent  (unavailable: executable_missing)"]


async def test_an_unavailable_agent_cannot_be_chosen() -> None:
    launcher = FakeLauncher()
    app = RemoteAgentsTui(_context(launcher=launcher))

    async with app.run_test() as pilot:
        app._choose_project("opaque-existing")
        app._choose_profile("cursor-agent")
        await pilot.pause()
        status = _status(app)

    assert "cannot be launched" in status
    assert "executable_missing" in status
    assert launcher.commands == []


async def test_review_names_the_project_agent_and_label_before_any_launch() -> None:
    launcher = FakeLauncher()
    app = RemoteAgentsTui(_context(launcher=launcher))

    async with app.run_test() as pilot:
        app._choose_project("opaque-existing")
        app._choose_profile("claude")
        app._submit_label("nightly run")
        await pilot.pause()
        status = _status(app)
        keys = _keys(app)

    assert "infra/existing" in status
    assert "claude" in status
    assert "nightly run" in status
    assert keys[:1] == ["launch"]
    assert launcher.commands == []


@pytest.mark.parametrize("value", ["", "   "])
async def test_an_empty_label_is_skipped_rather_than_rejected(value: str) -> None:
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        app._choose_project("opaque-existing")
        app._choose_profile("claude")
        app._submit_label(value)
        await pilot.pause()
        status = _status(app)

    assert "Label: none" in status


async def test_a_label_beyond_the_configured_bound_is_refused() -> None:
    app = RemoteAgentsTui(_context(max_label_length=10))

    async with app.run_test() as pilot:
        app._choose_project("opaque-existing")
        app._choose_profile("claude")
        app._submit_label("x" * 11)
        await pilot.pause()
        status = _status(app)

    assert "up to 10 characters" in status


async def test_cancel_at_review_returns_to_the_projects_without_launching() -> None:
    launcher = FakeLauncher()
    app = RemoteAgentsTui(_context(launcher=launcher))

    async with app.run_test() as pilot:
        app._choose_project("opaque-existing")
        app._choose_profile("claude")
        app._submit_label("")
        await app._resolve_review(_CANCEL)
        await pilot.pause()
        rows = _rows(app)

    assert launcher.commands == []
    assert rows == ["infra/existing  [Registered]", "dev-area/other-thing  [Unregistered]"]


async def test_back_at_review_restores_the_agent_choice() -> None:
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        app._choose_project("opaque-existing")
        app._choose_profile("claude")
        app._submit_label("")
        await app._resolve_review(_BACK)
        await pilot.pause()
        rows = _rows(app)

    assert rows == ["claude", "cursor-agent  (unavailable: executable_missing)"]


async def test_confirming_issues_one_launch_carrying_the_chosen_label() -> None:
    launcher = FakeLauncher()
    app = RemoteAgentsTui(_context(launcher=launcher))

    async with app.run_test():
        app._choose_project("opaque-existing")
        app._choose_profile("claude")
        app._submit_label("nightly")
        await app._resolve_review("launch")

    assert len(launcher.commands) == 1
    command = launcher.commands[0]
    assert str(command.project_id) == "opaque-existing"
    assert str(command.profile_id) == "claude"
    assert command.label == "nightly"
    assert command.idempotency_key.startswith("tui-")


async def test_two_launches_never_reuse_an_idempotency_key() -> None:
    launcher = FakeLauncher()
    keys = []
    for _ in range(2):
        app = RemoteAgentsTui(_context(launcher=launcher))
        async with app.run_test():
            app._choose_project("opaque-existing")
            app._choose_profile("claude")
            app._submit_label("")
            await app._resolve_review("launch")
    keys = [command.idempotency_key for command in launcher.commands]

    assert len(set(keys)) == 2


async def test_a_failed_launch_reports_and_returns_to_review_without_attaching() -> None:
    launcher = FakeLauncher(state=SessionState.FAILED)
    app = RemoteAgentsTui(_context(launcher=launcher))

    async with app.run_test() as pilot:
        app._choose_project("opaque-existing")
        app._choose_profile("claude")
        app._submit_label("")
        await app._resolve_review("launch")
        await pilot.pause()
        status = _status(app)
        keys = _keys(app)

    assert "did not become ready" in status
    assert keys[:1] == ["launch"]
    assert app.return_value is None


async def test_the_area_list_comes_from_the_creation_service() -> None:
    app = RemoteAgentsTui(_context(creator=FakeCreator(areas=("dev-area", "infra"))))

    async with app.run_test() as pilot:
        await app.action_add_project()
        await pilot.pause()
        keys = _keys(app)

    assert keys == ["dev-area", "infra", _CANCEL]


async def test_no_eligible_area_is_reported_rather_than_shown_empty() -> None:
    app = RemoteAgentsTui(_context(creator=FakeCreator(areas=())))

    async with app.run_test() as pilot:
        await app.action_add_project()
        await pilot.pause()
        status = _status(app)

    assert "No area is available" in status


@pytest.mark.parametrize("name", ["New Thing", "has space", "UPPER", "../escape", ""])
async def test_a_new_project_name_outside_the_slug_rule_creates_nothing(name: str) -> None:
    creator = FakeCreator()
    app = RemoteAgentsTui(_context(creator=creator))

    async with app.run_test() as pilot:
        await app.action_add_project()
        await app._choose_area("infra")
        app._submit_name(name)
        await pilot.pause()

    assert creator.commands == []


async def test_a_created_project_is_selectable_without_leaving_the_app() -> None:
    creator = FakeCreator()
    created = CatalogProject("opaque-new", "brand-new", "infra", "Registered")
    app = RemoteAgentsTui(_context(creator=creator, refresh_catalogue=lambda: (created,)))

    async with app.run_test() as pilot:
        await app.action_add_project()
        await app._choose_area("infra")
        app._submit_name("brand-new")
        await app._resolve_project_review("create")
        await pilot.pause()
        rows = _rows(app)

    assert creator.commands == [CreateProjectCommand("infra", "brand-new")]
    assert rows == ["infra/brand-new  [Registered]"]


async def test_a_refused_creation_is_reported_and_leaves_the_catalogue_alone() -> None:
    creator = FakeCreator(error=ProjectCreationError("project directory already exists"))
    app = RemoteAgentsTui(_context(creator=creator))

    async with app.run_test() as pilot:
        await app.action_add_project()
        await app._choose_area("infra")
        app._submit_name("brand-new")
        await app._resolve_project_review("create")
        await pilot.pause()
        status = _status(app)

    assert "Project not created" in status
    assert "already exists" in status


async def test_refresh_re_reads_a_project_another_process_created() -> None:
    later = CatalogProject("opaque-cli", "cli-made", "infra", "Registered")
    app = RemoteAgentsTui(_context(refresh_catalogue=lambda: (_EXISTING, later)))

    async with app.run_test() as pilot:
        await app.action_refresh()
        await pilot.pause()
        rows = _rows(app)

    assert "infra/cli-made  [Registered]" in rows


def test_label_normalisation_collapses_whitespace_and_bounds_length() -> None:
    assert label_or_error("  a   b  ", 40) == "a b"
    assert label_or_error("   ", 40) is None
    with pytest.raises(ValueError):
        label_or_error("x" * 41, 40)


def test_an_attach_request_carries_the_session_and_its_argument_vector() -> None:
    request = AttachRequest(
        "abc", ("tmux", "-L", "remote-agents", "attach-session", "-t", "=ra-abc")
    )

    assert request.session_id == "abc"
    assert "attach-session" in request.command
    assert request.argv[0] == "tmux"


async def test_the_keyboard_can_drive_a_launch_without_touching_a_private_method() -> None:
    """Private-method tests cannot see focus; only real keys prove the surface is usable."""
    launcher = FakeLauncher()
    app = RemoteAgentsTui(_context(launcher=launcher))

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert _keys(app) == ["claude", "cursor-agent"]

        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert [key for key in _keys(app)] == ["launch", "\x00back", "\x00cancel"]

        await pilot.press("up")
        await pilot.press("enter")
        await pilot.pause()

    assert len(launcher.commands) == 1
    assert str(launcher.commands[0].project_id) == "opaque-existing"


async def test_every_choice_list_hands_the_keyboard_to_the_list() -> None:
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        assert app.query_one("#filter").has_focus

        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#choices").has_focus and app.query_one("#choices").highlighted == 0

        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#choices").has_focus

        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#filter").has_focus

        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#choices").has_focus


async def test_the_add_project_binding_opens_the_area_list() -> None:
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()

        assert _keys(app)[:2] == ["dev-area", "infra"]
        assert app.query_one("#choices").has_focus


async def test_the_refresh_binding_re_reads_the_catalogue() -> None:
    later = CatalogProject("opaque-cli", "cli-made", "infra", "Registered")
    app = RemoteAgentsTui(_context(refresh_catalogue=lambda: (later,)))

    async with app.run_test() as pilot:
        await pilot.press("ctrl+r")
        await pilot.pause()

        assert _rows(app) == ["infra/cli-made  [Registered]"]


async def test_typing_a_new_project_name_reviews_it_before_creating_anything() -> None:
    creator = FakeCreator()
    app = RemoteAgentsTui(_context(creator=creator))

    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#filter").has_focus

        app.query_one("#filter").value = "typed-name"
        await pilot.press("enter")
        await pilot.pause()

        assert creator.commands == []
        assert _keys(app) == ["create", "\x00back", "\x00cancel"]
        assert "typed-name" in _status(app)

        await pilot.press("up")
        await pilot.press("enter")
        await pilot.pause()

    assert creator.commands == [CreateProjectCommand("dev-area", "typed-name")]


async def test_cancelling_the_new_project_review_creates_nothing() -> None:
    creator = FakeCreator()
    app = RemoteAgentsTui(_context(creator=creator))

    async with app.run_test() as pilot:
        await app.action_add_project()
        await app._choose_area("infra")
        app._submit_name("typed-name")
        await app._resolve_project_review(_CANCEL)
        await pilot.pause()

    assert creator.commands == []


async def test_an_area_the_identity_rule_rejects_is_never_offered() -> None:
    app = RemoteAgentsTui(_context(creator=FakeCreator(areas=("infra", "Not_A_Slug", "web"))))

    async with app.run_test() as pilot:
        await app.action_add_project()
        await pilot.pause()
        keys = _keys(app)

    assert keys[:2] == ["infra", "web"]


async def test_a_launch_failure_outside_the_error_contract_does_not_kill_the_app() -> None:
    class Exploding(FakeLauncher):
        async def launch(self, command: LaunchCommand) -> FakeRecord:
            raise RuntimeError("the terminal port broke its contract")

    app = RemoteAgentsTui(_context(launcher=Exploding()))

    async with app.run_test() as pilot:
        app._choose_project("opaque-existing")
        app._choose_profile("claude")
        app._submit_label("")
        await app._resolve_review("launch")
        await pilot.pause()
        status = _status(app)

    assert "was not started" in status
    assert app.return_value is None


async def test_a_failed_launch_still_names_a_way_to_reach_its_pane() -> None:
    """A launch that never reported ready may still have left a pane running."""
    app = RemoteAgentsTui(_context(launcher=FakeLauncher(state=SessionState.FAILED)))

    async with app.run_test() as pilot:
        app._choose_project("opaque-existing")
        app._choose_profile("claude")
        app._submit_label("")
        await app._resolve_review("launch")
        await pilot.pause()
        status = _status(app)

    assert "attach-session" in status
    assert "did not become ready" in status
    assert app.return_value is None
