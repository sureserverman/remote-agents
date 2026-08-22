"""Unit tests for the owner-only Telegram Add Project wizard."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from backends import backend_for
from fake_telegram import LoneMessageBot

from remote_agents.adapters.telegram.service import (
    PrivateBotBoundary,
    _TextEntry,
    build_private_bot,
)
from remote_agents.application.errors import ProjectCreationError
from remote_agents.application.project_admin import CreatedProject, CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.projects import ProjectIdentity

OWNER = 4242
CHAT = 8484
MESSAGE = 100


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

    def __init__(self, text: str = "", message_id: int = 1) -> None:
        self.text = text
        self.message_id = message_id
        self.replies: list[dict[str, object]] = []
        self.deletions: list[int] = []
        self.documents: list[dict[str, object]] = []
        self.bot = LoneMessageBot(self)

    def get_bot(self) -> LoneMessageBot:
        return self.bot

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


def _boundary(creator: FakeCreator | None = None, **wiring: object) -> PrivateBotBoundary:
    """`wiring` goes to the backend, which is where the use cases live now."""
    return build_private_bot(OWNER, CHAT, backend=backend_for(projects=creator, **wiring))


def _buttons(rendered: dict[str, object]) -> list[str]:
    markup = rendered.get("reply_markup")
    if markup is None:
        return []
    return [button.text for row in markup.inline_keyboard for button in row]


async def _send(boundary: PrivateBotBoundary, text: str) -> dict[str, object]:
    message = FakeMessage(text)
    await boundary.text(FakeUpdate(message), None)
    return message.replies[-1] if message.replies else {}


async def test_the_launch_list_offers_add_project_only_when_a_creator_is_configured() -> None:
    """Home used to carry this. Task 2.2 moved it to the launch picker; the gating claim is
    unchanged and is pinned on whichever screen offers the button."""
    with_creator = _boundary(FakeCreator())._projects_reply((), view_id="all")
    without_creator = _boundary()._projects_reply((), view_id="all")

    labels = lambda rendered: [b.text for row in rendered.keyboard for b in row]  # noqa: E731
    assert "Add Project" in labels(with_creator)
    assert "Add Project" not in labels(without_creator)


async def test_area_choices_come_from_the_server_not_from_typed_text() -> None:
    boundary = _boundary(FakeCreator(areas=("dev-area", "infra")))

    rendered = await boundary._reply_for("project.open", "areas")

    assert _buttons(rendered) == ["dev-area", "infra", "Cancel", "Sessions", "Launch"]


async def test_an_area_that_the_identity_rule_rejects_is_never_offered() -> None:
    boundary = _boundary(FakeCreator(areas=("infra", "Not_A_Slug", "big-projects")))

    rendered = await boundary._reply_for("project.open", "areas")

    assert _buttons(rendered) == ["infra", "big-projects", "Cancel", "Sessions", "Launch"]


async def test_an_empty_area_list_is_reported_rather_than_rendered_blank() -> None:
    boundary = _boundary(FakeCreator(areas=()))

    rendered = await boundary._reply_for("project.open", "areas")

    assert "No area is available" in str(rendered["text"])
    assert _buttons(rendered) == ["Sessions", "Launch"]


@pytest.mark.parametrize(
    "name", ["New Thing", "has space", "UPPER", "../escape", "trailing-", "", "under_score"]
)
async def test_a_name_outside_the_slug_rule_is_refused_before_any_effect(name: str) -> None:
    creator = FakeCreator()
    boundary = _boundary(creator)
    boundary._awaiting_text[(OWNER, CHAT)] = _TextEntry("project.name", "infra")

    rendered = await _send(boundary, name)

    assert "lowercase letters" in str(rendered["text"])
    assert creator.commands == []
    still_asking = boundary._awaiting_text[(OWNER, CHAT)]
    assert (still_asking.action, still_asking.entity_id) == ("project.name", "infra"), (
        "a refused name leaves the step open; only the prompt it replied to is replaced"
    )


async def test_a_valid_name_reaches_review_without_creating_anything() -> None:
    creator = FakeCreator()
    boundary = _boundary(creator)
    boundary._awaiting_text[(OWNER, CHAT)] = _TextEntry("project.name", "infra")

    rendered = await _send(boundary, "new-project")

    assert "Review new project" in str(rendered["text"])
    assert "infra" in str(rendered["text"])
    assert "new-project" in str(rendered["text"])
    assert _buttons(rendered) == ["Create", "Back", "Cancel", "Sessions", "Launch"]
    assert creator.commands == []
    assert (OWNER, CHAT) not in boundary._awaiting_text


@pytest.mark.parametrize("reply", ["Cancel", "cancel", "Back", "back"])
async def test_cancel_and_back_leave_name_entry_without_creating(reply: str) -> None:
    creator = FakeCreator()
    boundary = _boundary(creator)
    boundary._awaiting_text[(OWNER, CHAT)] = _TextEntry("project.name", "infra")

    rendered = await _send(boundary, reply)

    # Back to the area picker that asked for the name, not out of the wizard. The prompt
    # offers these two words to leave *this step*; a word that leaves the whole flow is not
    # the word the owner was offered.
    assert "Add project" in str(rendered["text"])
    assert creator.commands == []
    assert (OWNER, CHAT) not in boundary._awaiting_text


async def test_confirming_creates_exactly_once_even_when_the_callback_is_replayed() -> None:
    creator = FakeCreator()
    boundary = _boundary(creator)
    token = boundary._callback("project.confirm", "infra|new-project", mutation=True)
    boundary.callbacks.bind_pending(CHAT, MESSAGE)

    first = await _confirm(boundary, "infra|new-project", token)
    replayed = await _confirm(boundary, "infra|new-project", token)

    assert "Project created" in str(first["text"])
    assert "already run" in str(replayed["text"])
    assert creator.commands == [CreateProjectCommand("infra", "new-project")]


async def test_a_refused_creation_is_reported_without_raising() -> None:
    creator = FakeCreator(error=ProjectCreationError("project directory already exists"))
    boundary = _boundary(creator)
    token = boundary._callback("project.confirm", "infra|new-project", mutation=True)
    boundary.callbacks.bind_pending(CHAT, MESSAGE)

    rendered = await _confirm(boundary, "infra|new-project", token)

    assert "Project not created" in str(rendered["text"])
    assert "already exists" in str(rendered["text"])


async def test_a_created_project_is_offered_by_launch_without_a_restart() -> None:
    creator = FakeCreator()
    created = CatalogProject("opaque-new", "new-project", "infra", "Registered")
    boundary = _boundary(creator, refresh_catalogue=lambda: (created,))
    token = boundary._callback("project.confirm", "infra|new-project", mutation=True)

    await boundary._reply_for("project.confirm", "infra|new-project", token=token)
    launch = await boundary._reply_for("launch.open", "projects")

    assert boundary.catalogue == (created,)
    assert "new-project" in str(launch["text"]) or "new-project" in " ".join(_buttons(launch))


async def test_opening_launch_re_reads_a_project_created_by_another_process() -> None:
    """The command line writes from a separate process, so opening a picker must re-read.

    This used to be Home's Refresh, which is why the read is asserted as absent on `nav.home`
    first: the dashboard shows no projects, so re-walking the registry to draw it was work
    for a screen that could not display the result. The picker is where the answer is used.
    """
    created = CatalogProject("opaque-new", "cli-made", "infra", "Registered")
    reads: list[str] = []

    def source() -> tuple[CatalogProject, ...]:
        reads.append("read")
        return (created,)

    boundary = _boundary(FakeCreator(), refresh_catalogue=source)

    await boundary._reply_for("nav.home", "home")
    assert reads == []

    await boundary._reply_for("launch.open", "projects")
    assert reads == ["read"]
    assert boundary.catalogue == (created,)


async def test_a_confirmation_without_a_resolvable_selection_creates_nothing() -> None:
    creator = FakeCreator()
    boundary = _boundary(creator)
    token = boundary._callback("project.confirm", "malformed-entity", mutation=True)
    boundary.callbacks.bind_pending(CHAT, MESSAGE)

    rendered = await _confirm(boundary, "malformed-entity", token)

    assert "already run" in str(rendered["text"])
    assert creator.commands == []


async def test_add_project_actions_are_inert_without_a_creator() -> None:
    boundary = _boundary()
    token = boundary._callback("project.confirm", "infra|new-project", mutation=True)
    boundary.callbacks.bind_pending(CHAT, MESSAGE)

    areas = await boundary._reply_for("project.open", "areas")
    confirm = await _confirm(boundary, "infra|new-project", token)

    assert "unavailable" in str(areas["text"])
    # Not "already run": nothing ran, and with no creator wired nothing ever could.
    assert "Adding a project is unavailable." in str(confirm["text"])


async def test_name_entry_ignores_a_sender_who_is_not_the_owner() -> None:
    creator = FakeCreator()
    boundary = _boundary(creator)
    boundary._awaiting_text[(OWNER, CHAT)] = _TextEntry("project.name", "infra")
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
    token = boundary._callback("project.confirm", "infra|new-project", mutation=True)
    boundary.callbacks.bind_pending(CHAT, MESSAGE)

    rendered = await _confirm(boundary, "infra|new-project", token)

    assert "Project not created" in str(rendered["text"])
    assert "an adapter broke its contract" not in str(rendered["text"])


async def test_confirming_with_a_token_this_message_never_carried_does_not_raise() -> None:
    boundary = _boundary(FakeCreator())

    rendered = await _confirm(boundary, "infra|new-project", "c1_absent")

    assert "already run" in str(rendered["text"])


async def _confirm(boundary: PrivateBotBoundary, entity_id: str, token: str) -> dict[str, object]:
    """Press Create the way the callback dispatcher would, from the message that drew it."""
    return await boundary._reply_for("project.confirm", entity_id, token=token, message_id=MESSAGE)
