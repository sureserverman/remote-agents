"""Live Telegram service composition is owner-only and CLI-addressable without a network."""

from __future__ import annotations

from types import SimpleNamespace

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
