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


def test_doctor_uses_the_private_default_config_and_reports_operational_components(
    tmp_path, monkeypatch, capsys
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
    paths = _DoctorPaths(config)
    monkeypatch.setattr("remote_agents.bootstrap.ProductionPaths.for_home", lambda _home: paths)
    monkeypatch.setattr("remote_agents.bootstrap.database_is_ready", lambda _path: True)
    monkeypatch.setattr("remote_agents.bootstrap._command_succeeds", lambda _argv: True)
    monkeypatch.setattr(
        "remote_agents.bootstrap._telegram_credentials_are_private", lambda _paths: True
    )
    monkeypatch.setattr(
        "remote_agents.bootstrap.load_registry",
        lambda _path: SimpleNamespace(projects=(), error=None),
    )
    monkeypatch.setattr("remote_agents.bootstrap.discover_projects", lambda _path: ())
    monkeypatch.setattr("remote_agents.bootstrap.build_catalogue", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        "remote_agents.bootstrap.probe_profiles",
        lambda *_args, **_kwargs: (
            _compatibility("claude"),
            _compatibility("claude-remote"),
            _compatibility("codex"),
            _compatibility("opencode"),
            _compatibility("cursor-agent"),
        ),
    )

    assert main(["doctor", "--json"]) == 0

    report = __import__("json").loads(capsys.readouterr().out)
    assert report["healthy"] is True
    assert set(report["components"]) == {"core", "store", "tmux", "telegram", "service", "profiles"}
    assert [profile["status"] for profile in report["profiles"]] == ["QUALIFIED"] * 5


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


@pytest.mark.asyncio
async def test_private_bot_boundary_searches_projects_and_labels_a_launch() -> None:
    launcher = _Launcher()
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=(
            CatalogProject("a" * 24, "opaque-editor", "writing", "Registered"),
            CatalogProject("b" * 24, "opaque-verse", "writing", "Registered"),
        ),
        profiles=(ProfileAvailability("codex", True),),
        launcher=launcher,
    )
    message = _Message()
    await boundary.start(_trusted_update(message=message), None)
    launch = message.replies[0]["reply_markup"].inline_keyboard[0][0].callback_data
    projects = _Callback(launch)
    await boundary.callback(_trusted_update(callback=projects), None)
    search = projects.edits[0]["reply_markup"].inline_keyboard[2][0].callback_data
    awaiting_search = _Callback(search)
    await boundary.callback(_trusted_update(callback=awaiting_search), None)

    result = _Message("verse")
    await boundary.text(_trusted_update(message=result), None)

    project = result.replies[0]["reply_markup"].inline_keyboard[0][0].callback_data
    profiles = _Callback(project)
    await boundary.callback(_trusted_update(callback=profiles), None)
    profile = profiles.edits[0]["reply_markup"].inline_keyboard[0][0].callback_data
    confirmation = _Callback(profile)
    await boundary.callback(_trusted_update(callback=confirmation), None)
    label = confirmation.edits[0]["reply_markup"].inline_keyboard[1][0].callback_data
    awaiting_label = _Callback(label)
    await boundary.callback(_trusted_update(callback=awaiting_label), None)

    labelled = _Message("  review  ")
    await boundary.text(_trusted_update(message=labelled), None)
    confirm = labelled.replies[0]["reply_markup"].inline_keyboard[0][0].callback_data
    submitted = _Callback(confirm)
    await boundary.callback(_trusted_update(callback=submitted), None)

    assert str(launcher.commands[0].project_id) == "b" * 24
    assert launcher.commands[0].label == "review"


@pytest.mark.asyncio
async def test_private_bot_boundary_pages_through_the_entire_project_catalogue() -> None:
    catalogue = tuple(
        CatalogProject(f"{number:024d}", f"Project {number}", "tests", "Registered")
        for number in range(25)
    )
    boundary = PrivateBotBoundary(7, 11, catalogue=catalogue, project_page_size=10)
    message = _Message()
    await boundary.start(_trusted_update(message=message), None)
    launch = message.replies[0]["reply_markup"].inline_keyboard[0][0].callback_data
    first = _Callback(launch)
    await boundary.callback(_trusted_update(callback=first), None)

    first_page = first.edits[0]["reply_markup"].inline_keyboard
    assert first.edits[0]["text"] == "<b>Projects 1/3</b>\nSelect a project to launch."
    assert [row[0].text for row in first_page[:10]] == [f"Project {number}" for number in range(10)]
    second = _Callback(
        next(button.callback_data for row in first_page for button in row if button.text == "Next")
    )
    await boundary.callback(_trusted_update(callback=second), None)

    second_page = second.edits[0]["reply_markup"].inline_keyboard
    assert [row[0].text for row in second_page[:10]] == [
        f"Project {number}" for number in range(10, 20)
    ]
    third = _Callback(
        next(button.callback_data for row in second_page for button in row if button.text == "Next")
    )
    await boundary.callback(_trusted_update(callback=third), None)

    third_page = third.edits[0]["reply_markup"].inline_keyboard
    assert [row[0].text for row in third_page[:5]] == [
        f"Project {number}" for number in range(20, 25)
    ]


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


class _DoctorPaths:
    def __init__(self, config_path) -> None:
        self.config_path = config_path
        self.home = config_path.parent


def _compatibility(profile_id: str):
    from remote_agents.domain.profiles import ProfileCompatibility

    return ProfileCompatibility(ProfileId(profile_id), True, "1.2.3", "QUALIFIED", "verified")


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

    async def refresh_readiness(self) -> None:
        return None


class _Message:
    def __init__(self, text: str | None = None) -> None:
        self.replies: list[dict[str, object]] = []
        self.text = text

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
