"""Unit tests for the owner-only Telegram Add Project wizard."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.application.errors import ProjectCreationError
from remote_agents.application.project_admin import CreatedProject, CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.projects import ProjectIdentity

OWNER = 4242
CHAT = 8484


class FakeCreator:
    """Record every creation attempt so replay protection is observable."""

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


class FakeMessage:
    """Capture what the boundary would send without touching Telegram."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[dict[str, object]] = []

    async def reply_text(self, **arguments: object) -> None:
        self.replies.append(arguments)


class FakeUpdate:
    """Model only the owner/chat/message surface the boundary authorizes against."""

    def __init__(self, message: FakeMessage, *, user_id: int = OWNER, chat_id: int = CHAT) -> None:
        self.message = message
        self._user_id = user_id
        self._chat_id = chat_id

    @property
    def effective_user(self) -> object:
        return SimpleNamespace(id=self._user_id)

    @property
    def effective_chat(self) -> object:
        return SimpleNamespace(id=self._chat_id, type="private")

    @property
    def effective_message(self) -> FakeMessage:
        return self.message


def _boundary(creator: FakeCreator | None = None, **extra: object) -> PrivateBotBoundary:
    return PrivateBotBoundary(OWNER, CHAT, creator=creator, **extra)


def _buttons(rendered: dict[str, object]) -> list[str]:
    markup = rendered.get("reply_markup")
    if markup is None:
        return []
    return [button.text for row in markup.inline_keyboard for button in row]


async def _send(boundary: PrivateBotBoundary, text: str) -> dict[str, object]:
    message = FakeMessage(text)
    await boundary.text(FakeUpdate(message), None)
    return message.replies[-1] if message.replies else {}


async def test_home_offers_add_project_only_when_a_creator_is_configured() -> None:
    with_creator = await _boundary(FakeCreator())._home_reply()
    without_creator = await _boundary()._home_reply()

    assert "Add Project" in _buttons(with_creator)
    assert "Add Project" not in _buttons(without_creator)


async def test_area_choices_come_from_the_server_not_from_typed_text() -> None:
    boundary = _boundary(FakeCreator(areas=("dev-area", "infra")))

    rendered = await boundary._reply_for("project.open", "areas")

    assert _buttons(rendered) == ["dev-area", "infra", "Cancel", "Home"]


async def test_an_area_that_the_identity_rule_rejects_is_never_offered() -> None:
    boundary = _boundary(FakeCreator(areas=("infra", "Not_A_Slug", "big-projects")))

    rendered = await boundary._reply_for("project.open", "areas")

    assert _buttons(rendered) == ["infra", "big-projects", "Cancel", "Home"]


async def test_an_empty_area_list_is_reported_rather_than_rendered_blank() -> None:
    boundary = _boundary(FakeCreator(areas=()))

    rendered = await boundary._reply_for("project.open", "areas")

    assert "No area is available" in str(rendered["text"])
    assert _buttons(rendered) == ["Home"]


@pytest.mark.parametrize(
    "name", ["New Thing", "has space", "UPPER", "../escape", "trailing-", "", "under_score"]
)
async def test_a_name_outside_the_slug_rule_is_refused_before_any_effect(name: str) -> None:
    creator = FakeCreator()
    boundary = _boundary(creator)
    boundary._awaiting_text[(OWNER, CHAT)] = ("project.name", "infra")

    rendered = await _send(boundary, name)

    assert "lowercase letters" in str(rendered["text"])
    assert creator.commands == []
    assert boundary._awaiting_text[(OWNER, CHAT)] == ("project.name", "infra")


async def test_a_valid_name_reaches_review_without_creating_anything() -> None:
    creator = FakeCreator()
    boundary = _boundary(creator)
    boundary._awaiting_text[(OWNER, CHAT)] = ("project.name", "infra")

    rendered = await _send(boundary, "new-project")

    assert "Review new project" in str(rendered["text"])
    assert "infra" in str(rendered["text"])
    assert "new-project" in str(rendered["text"])
    assert _buttons(rendered) == ["Create", "Back", "Cancel", "Home"]
    assert creator.commands == []
    assert (OWNER, CHAT) not in boundary._awaiting_text


@pytest.mark.parametrize("reply", ["Cancel", "cancel", "Back", "back"])
async def test_cancel_and_back_leave_name_entry_without_creating(reply: str) -> None:
    creator = FakeCreator()
    boundary = _boundary(creator)
    boundary._awaiting_text[(OWNER, CHAT)] = ("project.name", "infra")

    rendered = await _send(boundary, reply)

    assert "Remote agents" in str(rendered["text"])
    assert creator.commands == []
    assert (OWNER, CHAT) not in boundary._awaiting_text


async def test_confirming_creates_exactly_once_even_when_the_callback_is_replayed() -> None:
    creator = FakeCreator()
    boundary = _boundary(creator)
    boundary._next_revision(OWNER, CHAT)
    token = boundary._callback("project.confirm", "infra|new-project", mutation=True)

    first = await boundary._reply_for("project.confirm", "infra|new-project", token=token)
    replayed = await boundary._reply_for("project.confirm", "infra|new-project", token=token)

    assert "Project created" in str(first["text"])
    assert "expired" in str(replayed["text"])
    assert creator.commands == [CreateProjectCommand("infra", "new-project")]


async def test_a_refused_creation_is_reported_without_raising() -> None:
    creator = FakeCreator(error=ProjectCreationError("project directory already exists"))
    boundary = _boundary(creator)
    boundary._next_revision(OWNER, CHAT)
    token = boundary._callback("project.confirm", "infra|new-project", mutation=True)

    rendered = await boundary._reply_for("project.confirm", "infra|new-project", token=token)

    assert "Project not created" in str(rendered["text"])
    assert "already exists" in str(rendered["text"])


async def test_a_created_project_is_offered_by_launch_without_a_restart() -> None:
    creator = FakeCreator()
    created = CatalogProject("opaque-new", "new-project", "infra", "Registered")
    boundary = _boundary(creator, catalogue_source=lambda: (created,))
    boundary._next_revision(OWNER, CHAT)
    token = boundary._callback("project.confirm", "infra|new-project", mutation=True)

    await boundary._reply_for("project.confirm", "infra|new-project", token=token)
    launch = await boundary._reply_for("launch.open", "projects")

    assert boundary.catalogue == (created,)
    assert "new-project" in str(launch["text"]) or "new-project" in " ".join(_buttons(launch))


async def test_refreshing_home_re_reads_a_project_created_by_another_process() -> None:
    """The command line writes from a separate process, so Home refresh must re-read."""
    created = CatalogProject("opaque-new", "cli-made", "infra", "Registered")
    reads: list[str] = []

    def source() -> tuple[CatalogProject, ...]:
        reads.append("read")
        return (created,)

    boundary = _boundary(FakeCreator(), catalogue_source=source)

    await boundary._reply_for("nav.home", "home")
    assert reads == []

    await boundary._reply_for("nav.refresh", "home")
    assert reads == ["read"]
    assert boundary.catalogue == (created,)


async def test_a_confirmation_without_a_resolvable_selection_creates_nothing() -> None:
    creator = FakeCreator()
    boundary = _boundary(creator)
    boundary._next_revision(OWNER, CHAT)
    token = boundary._callback("project.confirm", "malformed-entity", mutation=True)

    rendered = await boundary._reply_for("project.confirm", "malformed-entity", token=token)

    assert "expired" in str(rendered["text"])
    assert creator.commands == []


async def test_add_project_actions_are_inert_without_a_creator() -> None:
    boundary = _boundary()
    boundary._next_revision(OWNER, CHAT)
    token = boundary._callback("project.confirm", "infra|new-project", mutation=True)

    areas = await boundary._reply_for("project.open", "areas")
    confirm = await boundary._reply_for("project.confirm", "infra|new-project", token=token)

    assert "unavailable" in str(areas["text"])
    assert "expired" in str(confirm["text"])


async def test_name_entry_ignores_a_sender_who_is_not_the_owner() -> None:
    creator = FakeCreator()
    boundary = _boundary(creator)
    boundary._awaiting_text[(OWNER, CHAT)] = ("project.name", "infra")
    message = FakeMessage("new-project")

    await boundary.text(FakeUpdate(message, user_id=OWNER + 1), None)
    await boundary.text(FakeUpdate(message, chat_id=CHAT + 1), None)

    assert message.replies == []
    assert creator.commands == []


def test_every_add_project_action_is_behind_the_single_owner_chat_gate() -> None:
    """The callback dispatcher authorizes before any action runs, so prove the gate itself."""
    boundary = _boundary(FakeCreator())

    assert boundary.permits(FakeUpdate(FakeMessage()))
    assert not boundary.permits(FakeUpdate(FakeMessage(), user_id=OWNER + 1))
    assert not boundary.permits(FakeUpdate(FakeMessage(), chat_id=CHAT + 1))


async def test_a_failure_outside_the_error_contract_is_reported_not_dropped() -> None:
    creator = FakeCreator(error=RuntimeError("an adapter broke its contract"))
    boundary = _boundary(creator)
    boundary._next_revision(OWNER, CHAT)
    token = boundary._callback("project.confirm", "infra|new-project", mutation=True)

    rendered = await boundary._reply_for("project.confirm", "infra|new-project", token=token)

    assert "Project not created" in str(rendered["text"])
    assert "an adapter broke its contract" not in str(rendered["text"])


async def test_confirming_before_any_view_revision_exists_does_not_raise() -> None:
    boundary = _boundary(FakeCreator())

    rendered = await boundary._reply_for("project.confirm", "infra|new-project", token="c1_absent")

    assert "expired" in str(rendered["text"])
