"""Live Telegram service composition is owner-only and CLI-addressable without a network."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.bootstrap import _resolve_profile_executable, main
from remote_agents.config import TelegramSecrets
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


def test_private_bot_boundary_accepts_only_the_exact_configured_private_chat() -> None:
    boundary = PrivateBotBoundary(7, 11)
    trusted = SimpleNamespace(
        effective_user=SimpleNamespace(id=7), effective_chat=SimpleNamespace(id=11, type="private")
    )
    foreign_user = SimpleNamespace(
        effective_user=SimpleNamespace(id=8), effective_chat=SimpleNamespace(id=11, type="private")
    )
    group = SimpleNamespace(
        effective_user=SimpleNamespace(id=7), effective_chat=SimpleNamespace(id=11, type="group")
    )

    assert boundary.permits(trusted)
    assert not boundary.permits(foreign_user)
    assert not boundary.permits(group)


def test_profile_executable_resolver_includes_the_user_nvm_installation(tmp_path) -> None:
    executable = tmp_path / ".nvm" / "versions" / "node" / "v22" / "bin" / "codex"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)

    assert _resolve_profile_executable("codex", tmp_path) == executable


@pytest.mark.asyncio
async def test_private_bot_boundary_renders_and_refreshes_only_issued_owner_callbacks() -> None:
    boundary = PrivateBotBoundary(7, 11)
    message = _Message()
    update = _trusted_update(message=message)

    await boundary.start(update, None)

    assert message.replies[0]["text"] == "<b>Remote agents</b>\nChoose an action."
    refresh = message.replies[0]["reply_markup"].inline_keyboard[2][0].callback_data
    callback = _Callback(refresh)
    await boundary.callback(_trusted_update(callback=callback), None)

    assert callback.answers == [None]
    assert callback.edits[0]["text"] == "<b>Remote agents</b>\nChoose an action."

    await boundary.callback(_trusted_update(callback=callback), None)

    assert callback.answers == [None, "This view has expired."]
    assert len(callback.edits) == 1


@pytest.mark.asyncio
async def test_private_bot_boundary_submits_only_a_confirmed_opaque_launch() -> None:
    launcher = _Launcher()
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),),
        profiles=(ProfileAvailability("claude", True),),
        launcher=launcher,
    )
    message = _Message()
    await boundary.start(_trusted_update(message=message), None)
    launch = message.replies[0]["reply_markup"].inline_keyboard[0][0].callback_data
    projects = _Callback(launch)
    await boundary.callback(_trusted_update(callback=projects), None)
    project = projects.edits[0]["reply_markup"].inline_keyboard[0][0].callback_data
    profiles = _Callback(project)
    await boundary.callback(_trusted_update(callback=profiles), None)
    profile = profiles.edits[0]["reply_markup"].inline_keyboard[0][0].callback_data
    confirmation = _Callback(profile)
    await boundary.callback(_trusted_update(callback=confirmation), None)
    confirm = confirmation.edits[0]["reply_markup"].inline_keyboard[0][0].callback_data
    submitted = _Callback(confirm)
    await boundary.callback(_trusted_update(callback=submitted), None)

    assert len(launcher.commands) == 1
    assert str(launcher.commands[0].project_id) == "a" * 24
    assert str(launcher.commands[0].profile_id) == "claude"


@pytest.mark.asyncio
async def test_private_bot_boundary_ignores_a_duplicate_telegram_edit() -> None:
    boundary = PrivateBotBoundary(7, 11)
    message = _Message()
    await boundary.start(_trusted_update(message=message), None)
    refresh = message.replies[0]["reply_markup"].inline_keyboard[2][0].callback_data
    callback = _Callback(refresh, edit_error=BadRequest("Message is not modified"))

    await boundary.callback(_trusted_update(callback=callback), None)

    assert callback.answers == [None]


@pytest.mark.asyncio
async def test_private_bot_boundary_hides_ended_history_from_sessions_list() -> None:
    launcher = _Launcher()
    launcher.records = [
        _record(SessionState.RUNNING, "active", ProjectId("a" * 24)),
        _record(SessionState.ENDED, "ended", ProjectId("a" * 24)),
    ]
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),),
        launcher=launcher,
    )

    reply = await boundary._sessions_reply()

    labels = tuple(button.text for row in reply.keyboard for button in row)
    assert labels == ("Demo · codex · regular · #1 · active", "Home")
    assert "ended" not in labels


def test_serve_command_loads_config_and_runs_the_injected_private_bot(
    tmp_path, monkeypatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "[paths]\n"
        f'dev_root = "{tmp_path}"\n'
        f'registry_path = "{tmp_path / "registry.yaml"}"\n'
        f'database_path = "{tmp_path / "sessions.sqlite3"}"\n\n'
        "[limits]\nmax_label_length = 40\nproject_page_size = 10\n",
        encoding="utf-8",
    )
    received: list[TelegramSecrets] = []

    async def serve(secrets: TelegramSecrets, _boundary: PrivateBotBoundary) -> None:
        received.append(secrets)

    monkeypatch.setattr(
        "remote_agents.bootstrap.load_secrets", lambda: TelegramSecrets("token", 7, 11)
    )
    monkeypatch.setattr(
        "remote_agents.bootstrap.ProductionPaths.for_home",
        lambda _home: _Paths(tmp_path / "sessions.sqlite3"),
    )
    monkeypatch.setattr(
        "remote_agents.bootstrap._private_boundary",
        lambda _config, _connection, _paths: PrivateBotBoundary(7, 11),
    )

    assert main(["serve", "--config", str(config)], serve_runner=serve) == 0
    assert received == [TelegramSecrets("token", 7, 11)]


class _Paths:
    def __init__(self, database_path) -> None:
        self.database_path = database_path

    def ensure_directories(self) -> None:
        return None

    def require_private_environment(self):
        return None

    def open_database(self):
        return _Connection()


class _Connection:
    def close(self) -> None:
        return None


class _Launcher:
    def __init__(self) -> None:
        self.commands = []
        self.records = []

    async def launch(self, command) -> None:
        self.commands.append(command)

    async def list_sessions(self):
        return self.records


class _Message:
    def __init__(self) -> None:
        self.replies: list[dict[str, object]] = []

    async def reply_text(self, **kwargs: object) -> None:
        self.replies.append(kwargs)


class _Callback:
    def __init__(self, data: str, *, edit_error: Exception | None = None) -> None:
        self.data = data
        self.edit_error = edit_error
        self.answers: list[str | None] = []
        self.edits: list[dict[str, object]] = []

    async def answer(self, text: str | None = None) -> None:
        self.answers.append(text)

    async def edit_message_text(self, **kwargs: object) -> None:
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(kwargs)


def _trusted_update(*, message: _Message | None = None, callback: _Callback | None = None):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=11, type="private"),
        effective_message=message,
        callback_query=callback,
    )


def _record(state: SessionState, label: str, project_id: ProjectId) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        project_id,
        ProfileId("codex"),
        SessionDisplayIdentity("demo", "codex", "regular", 1, label),
        state,
        datetime.now(UTC),
    )
