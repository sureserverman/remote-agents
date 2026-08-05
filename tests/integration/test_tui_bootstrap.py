"""The local terminal composes over the same private store the service uses."""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.bootstrap import local_context, main
from remote_agents.config import ConfigError
from remote_agents.production import ProductionPaths

_REGISTRY = """version: 1
projects:
  - path: {existing}
    name: existing
    area: infra
    enabled: true
    added: 2026-07-30
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    root = tmp_path / "home"
    (root / "dev" / "infra" / "existing").mkdir(parents=True)
    return root


@pytest.fixture
def paths(home: Path) -> ProductionPaths:
    return ProductionPaths.for_home(home)


def _config_file(
    home: Path, paths: ProductionPaths, database: Path | None = None, stem: str = "config"
) -> Path:
    registry = home / "projects-registry.yaml"
    registry.write_text(
        _REGISTRY.format(existing=home / "dev" / "infra" / "existing"), encoding="utf-8"
    )
    config_path = home / f"{stem}.toml"
    config_path.write_text(
        f'[paths]\ndev_root = "{home / "dev"}"\n'
        f'registry_path = "{registry}"\n'
        f'database_path = "{database or paths.database_path}"\n\n'
        "[limits]\nmax_label_length = 40\nproject_page_size = 10\n",
        encoding="utf-8",
    )
    return config_path


def test_the_local_context_offers_the_catalogue_profiles_and_creation_service(
    home: Path, paths: ProductionPaths, tmp_path: Path
) -> None:
    from remote_agents.config import load_config

    config = load_config(_config_file(home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        context = local_context(config, connection, paths)

        assert isinstance(context, TuiContext)
        assert "existing" in {project.name for project in context.catalogue}
        assert {profile.profile_id for profile in context.profiles} == {
            "claude",
            "claude-remote",
            "codex",
            "cursor-agent",
            "opencode",
        }
        assert context.max_label_length == 40
        assert context.creator.available_areas() == ("infra",)
    finally:
        connection.close()


def test_the_local_context_needs_no_telegram_credentials(
    home: Path, paths: ProductionPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner runs this on their own host; nothing about it is a remote control plane."""
    from remote_agents.config import load_config

    for name in (
        "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN",
        "REMOTE_AGENTS_OWNER_USER_ID",
        "REMOTE_AGENTS_OWNER_CHAT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    config = load_config(_config_file(home, paths))
    connection = open_database(tmp_path / "sessions.sqlite3")
    try:
        assert local_context(config, connection, paths) is not None
    finally:
        connection.close()


def test_the_tui_command_refuses_a_database_outside_the_private_state_directory(
    home: Path, paths: ProductionPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminal shares the service's store, so it inherits the same confinement."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    config_path = _config_file(home, paths, database=tmp_path / "elsewhere.sqlite3")

    status = main(["tui", "--config", str(config_path)])

    assert status == 1


def test_serve_and_the_terminal_share_one_database_guard(
    home: Path, paths: ProductionPaths
) -> None:
    from remote_agents.bootstrap import _private_state_config

    outside = _config_file(home, paths, database=home / "elsewhere.sqlite3", stem="outside")
    inside = _config_file(home, paths, stem="inside")

    with pytest.raises(ConfigError):
        _private_state_config(outside, paths)
    assert _private_state_config(inside, paths).database_path == paths.database_path


async def test_two_connections_on_one_store_claim_distinct_keys(tmp_path: Path) -> None:
    """The service and the terminal are separate processes over one SQLite file."""
    database = tmp_path / "sessions.sqlite3"
    service = open_database(database)
    terminal = open_database(database)
    try:
        service_store = SQLiteSessionStore(service)
        terminal_store = SQLiteSessionStore(terminal)

        assert await service_store.claim_idempotency_key("service-1")
        assert await terminal_store.claim_idempotency_key("tui-1")
        assert not await terminal_store.claim_idempotency_key("service-1")
        assert not await service_store.claim_idempotency_key("tui-1")
    finally:
        service.close()
        terminal.close()


def test_textual_is_pinned_exactly_like_every_other_runtime_dependency() -> None:
    dependencies = (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")

    assert '"textual==8.2.8"' in dependencies
