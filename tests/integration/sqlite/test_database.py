"""SQLite migration, backup, rollback, and metadata-boundary integration tests."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import database_is_ready, open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS, current_version
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.services import SessionService
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

    assert current_version(connection) == 5
    names = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "sessions",
        "session_events",
        "idempotency_claims",
        "schema_version",
    } <= names


def test_database_health_rejects_incomplete_schema_version_table(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    connection.close()

    assert database_is_ready(path) is False


def test_upgrade_creates_backup_before_new_migration(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    sqlite3.connect(path).close()

    connection = open_database(path)

    assert current_version(connection) == 5
    assert path.with_suffix(".sqlite3.bak").exists()


def test_failed_migration_rolls_back_schema_version(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")

    with pytest.raises(sqlite3.OperationalError):
        open_database(
            tmp_path / "sessions.sqlite3",
            migrations=((1, ""), (2, ""), (3, ""), (4, ""), (5, ""), (6, "CREATE TABLE broken (")),
        )

    assert current_version(connection) == 5


def test_migration_five_adds_callback_state_tables_scoped_to_messages_not_clocks(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite3"
    open_database(path, migrations=MIGRATIONS[:4]).close()
    assert current_version(sqlite3.connect(path)) == 4

    connection = open_database(path)

    assert current_version(connection) == 5
    callback_columns = [row[1] for row in connection.execute("PRAGMA table_info(callback_states)")]
    assert callback_columns == [
        "token",
        "action",
        "entity_id",
        "owner_id",
        "chat_id",
        "message_id",
        "mutation",
        "claimed",
        "force_confirmed",
        "created_at",
    ]
    view_columns = [row[1] for row in connection.execute("PRAGMA table_info(chat_views)")]
    assert view_columns == ["chat_id", "message_id", "updated_at"]
    assert "expires_at" not in set(callback_columns) | set(view_columns)
    indexed = [row[2] for row in connection.execute("PRAGMA index_info(callback_states_message)")]
    assert indexed == ["chat_id", "message_id"]


async def test_store_uses_bound_values_append_only_events_and_unique_claims(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")
    store = SQLiteSessionStore(connection)
    session = record()
    await store.save(session)
    store.append_event(str(session.session_id), LifecycleEvent.READY, idempotency_key="ready-1")

    assert await store.claim_idempotency_key("callback-1") is True
    assert await store.claim_idempotency_key("callback-1") is False
    assert connection.execute("SELECT COUNT(*) FROM session_events").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM idempotency_claims").fetchone()[0] == 1
    columns = {row[1] for row in connection.execute("PRAGMA table_info(session_events)")}
    assert not {"pane", "prompt", "token", "environment"} & columns


async def test_sqlite_store_composes_with_the_async_session_service(tmp_path: Path) -> None:
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    service = SessionService(store, FakeTerminal())

    record = await service.launch(
        LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "launch-1")
    )

    assert record.state is SessionState.RUNNING
    assert await service.list_sessions() == (record,)
