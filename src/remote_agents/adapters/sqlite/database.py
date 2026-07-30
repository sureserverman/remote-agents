"""SQLite connection, pre-migration backup, and transactional migration boundary."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from remote_agents.adapters.sqlite.migrations import MIGRATIONS, apply_migrations, current_version


def open_database(
    path: Path, *, migrations: Iterable[tuple[int, str]] = MIGRATIONS
) -> sqlite3.Connection:
    """Open a metadata database and apply pending migrations with a prior backup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_backup = path.exists()
    connection = sqlite3.connect(path)
    try:
        before = current_version(connection)
        migrations = tuple(migrations)
        if any(version > before for version, _ in migrations) and needs_backup:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        apply_migrations(connection, migrations)
        return connection
    except Exception:
        connection.close()
        raise


def database_is_ready(path: Path) -> bool:
    """Check an existing database is readable and at the current schema version."""
    if not path.is_file():
        return False
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            return current_version(connection) == MIGRATIONS[-1][0]
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError):
        return False
