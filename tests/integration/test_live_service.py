"""Live Telegram service composition is owner-only and CLI-addressable without a network."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.bootstrap import main
from remote_agents.config import TelegramSecrets


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


@pytest.mark.asyncio
async def test_private_bot_boundary_renders_and_refreshes_only_issued_owner_callbacks() -> None:
    boundary = PrivateBotBoundary(7, 11)
    message = _Message()
    update = _trusted_update(message=message)

    await boundary.start(update, None)

    assert message.replies[0]["text"] == "<b>Remote agents</b>\nChoose an action."
    refresh = message.replies[0]["reply_markup"].inline_keyboard[0][0].callback_data
    callback = _Callback(refresh)
    await boundary.callback(_trusted_update(callback=callback), None)

    assert callback.answers == [None]
    assert callback.edits[0]["text"] == "<b>Remote agents</b>\nChoose an action."

    await boundary.callback(_trusted_update(callback=callback), None)

    assert callback.answers == [None, "This view has expired."]
    assert len(callback.edits) == 1


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

    async def serve(secrets: TelegramSecrets) -> None:
        received.append(secrets)

    monkeypatch.setattr(
        "remote_agents.bootstrap.load_secrets", lambda: TelegramSecrets("token", 7, 11)
    )
    monkeypatch.setattr(
        "remote_agents.bootstrap.ProductionPaths.for_home",
        lambda _home: _Paths(tmp_path / "sessions.sqlite3"),
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


class _Message:
    def __init__(self) -> None:
        self.replies: list[dict[str, object]] = []

    async def reply_text(self, **kwargs: object) -> None:
        self.replies.append(kwargs)


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[str | None] = []
        self.edits: list[dict[str, object]] = []

    async def answer(self, text: str | None = None) -> None:
        self.answers.append(text)

    async def edit_message_text(self, **kwargs: object) -> None:
        self.edits.append(kwargs)


def _trusted_update(*, message: _Message | None = None, callback: _Callback | None = None):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=11, type="private"),
        effective_message=message,
        callback_query=callback,
    )
