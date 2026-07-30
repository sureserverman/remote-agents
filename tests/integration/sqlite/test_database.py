"""SQLite migration, backup, rollback, and metadata-boundary integration tests."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import current_version
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.state_machine import LifecycleEvent


def record() -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.STARTING,
        datetime(2026, 7, 30, tzinfo=UTC),
    )


def test_clean_database_creates_versioned_projection_and_event_tables(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")

    assert current_version(connection) == 1
    names = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"sessions", "session_events", "schema_version"} <= names


def test_upgrade_creates_backup_before_new_migration(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    sqlite3.connect(path).close()

    connection = open_database(path)

    assert current_version(connection) == 1
    assert path.with_suffix(".sqlite3.bak").exists()


def test_failed_migration_rolls_back_schema_version(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")

    with pytest.raises(sqlite3.OperationalError):
        open_database(
            tmp_path / "sessions.sqlite3", migrations=((1, ""), (2, "CREATE TABLE broken ("))
        )

    assert current_version(connection) == 1


def test_store_uses_bound_values_append_only_events_and_unique_claims(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")
    store = SQLiteSessionStore(connection)
    session = record()
    store.save(session)
    store.append_event(str(session.session_id), LifecycleEvent.READY, idempotency_key="ready-1")

    assert store.claim_idempotency_key("callback-1") is True
    assert store.claim_idempotency_key("callback-1") is False
    assert connection.execute("SELECT COUNT(*) FROM session_events").fetchone()[0] == 2
    columns = {row[1] for row in connection.execute("PRAGMA table_info(session_events)")}
    assert not {"pane", "prompt", "token", "environment"} & columns
