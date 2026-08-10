"""The project a surface creates must be launchable through the ordinary wizard path."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_telegram import LoneMessageBot

from remote_agents.adapters.projects.registry_writer import RegistryProjectRecorder
from remote_agents.adapters.projects.workspace import FilesystemProjectWorkspace
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.telegram.service import PrivateBotBoundary, _TextEntry
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.application.project_admin import ProjectCreationService
from remote_agents.application.services import SessionService
from remote_agents.bootstrap import ProjectCatalogueProvider
from remote_agents.domain.models import SessionState

OWNER = 4242
CHAT = 8484

_REGISTRY = """version: 1
projects:
  - path: {existing}
    name: existing
    area: infra
    enabled: true
    added: 2026-07-30
"""


class FakeMessage:
    def __init__(self, text: str = "", message_id: int = 1) -> None:
        self.text = text
        self.message_id = message_id
        self.replies: list[dict[str, object]] = []
        self.deletions: list[int] = []
        self.bot = LoneMessageBot(self)

    def get_bot(self) -> LoneMessageBot:
        return self.bot

    async def reply_text(self, **arguments: object) -> None:
        self.replies.append(arguments)


class FakeUpdate:
    def __init__(self, message: FakeMessage) -> None:
        self.message = message

    @property
    def effective_user(self) -> object:
        return SimpleNamespace(id=OWNER)

    @property
    def effective_chat(self) -> object:
        return SimpleNamespace(id=CHAT, type="private")

    @property
    def effective_message(self) -> FakeMessage:
        return self.message


@pytest.fixture
def dev_root(tmp_path: Path) -> Path:
    root = tmp_path / "dev"
    (root / "infra" / "existing").mkdir(parents=True)
    return root


@pytest.fixture
def registry_path(tmp_path: Path, dev_root: Path) -> Path:
    path = tmp_path / "projects-registry.yaml"
    path.write_text(_REGISTRY.format(existing=dev_root / "infra" / "existing"), encoding="utf-8")
    return path


def _buttons(rendered: dict[str, object]) -> list[tuple[str, str]]:
    markup = rendered.get("reply_markup")
    if markup is None:
        return []
    return [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]


def _callback_for(rendered: dict[str, object], label: str) -> str:
    return next(data for text, data in _buttons(rendered) if text == label)


async def test_a_project_created_in_the_wizard_launches_through_the_ordinary_path(
    dev_root: Path, registry_path: Path, tmp_path: Path
) -> None:
    """Creating and launching share one catalogue, so prove the whole journey, not the seam."""
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        provider = ProjectCatalogueProvider(registry_path, dev_root)
        terminal = FakeTerminal()
        boundary = PrivateBotBoundary(
            OWNER,
            CHAT,
            catalogue=provider.refresh().catalogue,
            profiles=(ProfileAvailability("claude", True, None),),
            launcher=SessionService(SQLiteSessionStore(connection), terminal),
            creator=ProjectCreationService(
                FilesystemProjectWorkspace(dev_root),
                RegistryProjectRecorder(registry_path, dev_root, today=lambda: date(2026, 8, 5)),
            ),
            catalogue_source=lambda: provider.refresh().catalogue,
        )

        areas = await boundary._reply_for("project.open", "areas")
        assert ("infra", _callback_for(areas, "infra")) in _buttons(areas)
        boundary._awaiting_text[(OWNER, CHAT)] = _TextEntry("project.name", "infra")
        entry = FakeMessage("brand-new")
        await boundary.text(FakeUpdate(entry), None)
        review = entry.replies[-1]
        assert "brand-new" in str(review["text"])
        assert not (dev_root / "infra" / "brand-new").exists()

        # The token was drawn on the live view, so the press has to come from it. This
        # used to be pressed with message_id=0 and match anyway, because the double's
        # `reply_text` answered None, nothing ever bound the token, and UNBOUND is 0.
        created = await boundary._reply_for(
            "project.confirm",
            "infra|brand-new",
            token=_callback_for(review, "Create"),
            message_id=entry.message_id,
        )

        assert "Project created" in str(created["text"])
        assert (dev_root / "infra" / "brand-new").is_dir()

        projects = await boundary._reply_for("launch.open", "projects")
        opaque = next(
            project.opaque_id for project in boundary.catalogue if project.name == "brand-new"
        )
        assert any(data == _callback_for(projects, "brand-new") for _, data in _buttons(projects))
        await boundary._reply_for("launch.project", opaque)
        confirm = await boundary._reply_for("launch.profile", f"{opaque}|claude")
        launched = await boundary._reply_for(
            "launch.confirm", f"{opaque}|claude", token=_callback_for(confirm, "Launch")
        )

        assert "Session created" in str(launched["text"])
        records = await boundary.launcher.list_sessions()
        assert [record.state for record in records] == [SessionState.RUNNING]
        assert str(records[0].project_id) == opaque
    finally:
        connection.close()
