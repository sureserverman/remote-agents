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
        Path(".local/state/remote-agents/activity"),
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


def test_activity_directory_is_private_and_under_the_state_directory(tmp_path: Path) -> None:
    paths = ProductionPaths.for_home(tmp_path)

    paths.ensure_directories()

    assert paths.activity_directory == paths.state_directory / "activity"
    assert paths.activity_directory.is_dir()
    assert stat.S_IMODE(paths.activity_directory.stat().st_mode) == 0o700


def test_activity_directory_repairs_a_loosened_mode(tmp_path: Path) -> None:
    paths = ProductionPaths.for_home(tmp_path)
    paths.ensure_directories()
    paths.activity_directory.chmod(0o755)

    paths.ensure_directories()

    assert stat.S_IMODE(paths.activity_directory.stat().st_mode) == 0o700


def test_activity_directory_refuses_to_be_a_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    paths = ProductionPaths.for_home(tmp_path)
    paths.state_directory.mkdir(parents=True)
    paths.activity_directory.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigError, match="symlinks"):
        paths.ensure_directories()


def test_production_paths_refuse_a_symlinked_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".config").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigError, match="symlinks"):
        ProductionPaths.for_home(tmp_path).ensure_directories()


def test_preferences_path_is_under_the_state_directory_and_not_beside_the_config(
    tmp_path: Path,
) -> None:
    """A UI preference is state, not configuration.

    `config.toml` is the operator's hand-written file with an exact-key schema; a value the
    surface writes for itself has no business in it, and an unknown key there is a
    configuration error rather than a forgotten preference.
    """
    paths = ProductionPaths.for_home(tmp_path)

    assert paths.preferences_path == paths.state_directory / "preferences.json"
    assert paths.preferences_path.parent != paths.config_directory
    assert paths.preferences_path != paths.config_path


def test_preferences_path_is_not_created_by_ensure_directories(tmp_path: Path) -> None:
    """Like `console_lock_path`: the directory is declared, the file is the writer's.

    Nothing should have to have run for the surface to start, and a preferences file that
    exists but is empty is one of the cases the reader already forgives.
    """
    paths = ProductionPaths.for_home(tmp_path)

    paths.ensure_directories()

    assert paths.state_directory.is_dir()
    assert not paths.preferences_path.exists()
