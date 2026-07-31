"""SQLite connection, pre-migration backup, and transactional migration boundary."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from remote_agents.adapters.sqlite.migrations import MIGRATIONS, apply_migrations, current_version


def open_database(
    path: Path,
    *,
    migrations: Iterable[tuple[int, str]] = MIGRATIONS,
    busy_timeout_ms: int = 1_000,
) -> sqlite3.Connection:
    """Open a metadata database and apply pending migrations with a prior backup."""
    if busy_timeout_ms < 0:
        raise ValueError("database busy timeout cannot be negative")
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_backup = path.exists()
    connection = sqlite3.connect(path, timeout=busy_timeout_ms / 1_000)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    try:
        before = current_version(connection)
        migrations = tuple(migrations)
        if any(version > before for version, _ in migrations) and needs_backup:
            _backup_connection(connection, backup_path(path))
        apply_migrations(connection, migrations)
        return connection
    except Exception:
        connection.close()
        raise


def backup_path(path: Path) -> Path:
    """Return the single recoverable snapshot path belonging to one state database."""
    return path.with_suffix(path.suffix + ".bak")


def restore_database(path: Path, backup: Path | None = None) -> None:
    """Atomically restore a verified backup after preserving unreadable database evidence."""
    source = backup_path(path) if backup is None else backup
    if not database_is_ready(source):
        raise ValueError("database backup is not a readable current schema")
    if path.exists() and database_is_ready(path):
        raise ValueError("refusing to replace a healthy database")
    if path.exists():
        _preserve_corrupt_database(path)
    source_connection = _read_only_connection(source)
    try:
        _backup_connection(source_connection, path)
    finally:
        source_connection.close()


def _backup_connection(source: sqlite3.Connection, destination: Path) -> None:
    """Use SQLite's online backup API and atomically publish only a complete snapshot."""
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
    finally:
        target.close()
    os.replace(temporary, destination)


def _preserve_corrupt_database(path: Path) -> None:
    """Move the database and any SQLite sidecars together before replacing it."""
    corrupt = path.with_suffix(path.suffix + ".corrupt")
    sources = (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
    destinations = (corrupt, Path(f"{corrupt}-wal"), Path(f"{corrupt}-shm"))
    if any(destination.exists() for destination in destinations):
        raise FileExistsError("corrupt database evidence already exists")
    for source, destination in zip(sources, destinations, strict=True):
        if source.exists():
            os.replace(source, destination)


def _read_only_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def database_is_ready(path: Path) -> bool:
    """Check an existing database is readable and at the current schema version."""
    if not path.is_file():
        return False
    try:
        connection = _read_only_connection(path)
        try:
            return current_version(connection) == MIGRATIONS[-1][0]
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError):
        return False
