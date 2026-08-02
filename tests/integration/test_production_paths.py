"""Production-path integration checks use a temporary synthetic home directory."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.config import ConfigError
from remote_agents.production import ProductionPaths


def test_production_paths_create_only_private_declared_directories_and_database(
    tmp_path: Path,
) -> None:
    paths = ProductionPaths.for_home(tmp_path)

    connection = paths.open_database(open_database, migrations=MIGRATIONS)
    connection.close()

    assert paths.database_path.is_file()
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == {
        Path(".config"),
        Path(".config/remote-agents"),
        Path(".config/systemd"),
        Path(".config/systemd/user"),
        Path(".local"),
        Path(".local/state"),
        Path(".local/state/remote-agents"),
        Path(".local/state/remote-agents/intents"),
        Path(".local/state/remote-agents/sessions.sqlite3"),
    }
    for directory in (paths.config_directory, paths.state_directory, paths.unit_directory):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.database_path.stat().st_mode) == 0o600


def test_production_database_backup_precedes_an_upgrade(tmp_path: Path) -> None:
    paths = ProductionPaths.for_home(tmp_path)
    paths.open_database(
        open_database, migrations=((1, "CREATE TABLE first_table (id INTEGER);"),)
    ).close()

    paths.open_database(
        open_database,
        migrations=(
            (1, "CREATE TABLE first_table (id INTEGER);"),
            (2, "CREATE TABLE second_table (id INTEGER);"),
        ),
    ).close()

    assert paths.database_path.with_suffix(".sqlite3.bak").is_file()


def test_production_environment_must_be_owner_only(tmp_path: Path) -> None:
    paths = ProductionPaths.for_home(tmp_path)
    paths.ensure_directories()
    paths.environment_path.write_text("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=redacted\n")
    paths.environment_path.chmod(0o644)

    with pytest.raises(ConfigError, match="mode 0600"):
        paths.require_private_environment()

    paths.environment_path.chmod(0o600)

    assert paths.require_private_environment() == paths.environment_path


def test_production_paths_refuse_a_symlinked_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".config").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigError, match="symlinks"):
        ProductionPaths.for_home(tmp_path).ensure_directories()
