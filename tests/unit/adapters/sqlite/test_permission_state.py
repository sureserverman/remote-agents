"""A launch's permission request is durable before its pane exists, and its outcome beside it.

`session_permissions` is the immutable launch snapshot. `session_permission_state` is what the
service still owes the session: the preset it asked for, the one the daemon last confirmed, the
daemon that confirmed it, and the reason of a failure. A session without a state row was launched
before the service applied presets on the daemon, and nothing here touches it.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.permission_bindings import SQLitePermissionBindings
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.ports.permission_bindings import PermissionState

SESSION = SessionId("s-codex-1")
STATE_MIGRATION = 26
TABLES = ("sessions", "session_permissions", "session_permission_state")


def _record(session_id: SessionId = SESSION, profile: str = "codex") -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque-editor"),
        ProfileId(profile),
        SessionDisplayIdentity("opaque-editor", profile, "regular", 1),
        SessionState.RUNNING,
        datetime(2026, 11, 12, 9, 0, tzinfo=UTC),
    )


def _rows(connection: sqlite3.Connection, table: str) -> list[tuple]:
    return connection.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - a test constant


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    return open_database(tmp_path / "state.sqlite3", migrations=MIGRATIONS)


async def test_a_launch_writes_the_session_its_snapshot_and_a_pending_state_row(connection) -> None:
    await SQLiteSessionStore(connection).save(_record(), permissions=PermissionPreset.ASK)

    assert connection.execute(
        "SELECT preset FROM session_permissions WHERE session_id = ?", (str(SESSION),)
    ).fetchone() == ("ask",)
    assert connection.execute(
        "SELECT requested, applied, daemon, failure, updated_at FROM session_permission_state"
        " WHERE session_id = ?",
        (str(SESSION),),
    ).fetchone() == ("ask", None, None, None, "2026-11-12T09:00:00+00:00")
    assert await SQLitePermissionBindings(connection).state(SESSION) == PermissionState(
        requested=PermissionPreset.ASK, applied=None, daemon=None, failure=None
    )


@pytest.mark.parametrize("failing", ["session_permission_state", "session_project_roots"])
async def test_a_failed_save_leaves_no_session_no_snapshot_and_no_state(
    connection, failing: str
) -> None:
    """The three rows are one transaction: a statement failing at or after the state row takes
    the session and its snapshot with it, so no pane is ever launched over half a record."""
    connection.execute(
        f"CREATE TRIGGER refuse BEFORE INSERT ON {failing}"  # noqa: S608 - a test constant
        " BEGIN SELECT RAISE(ABORT, 'refused by the test'); END"
    )
    with pytest.raises(sqlite3.DatabaseError, match="refused by the test"):
        await SQLiteSessionStore(connection).save(
            _record(), permissions=PermissionPreset.FULL_ACCESS, project_root=Path("/srv/project")
        )

    assert {table: _rows(connection, table) for table in TABLES} == dict.fromkeys(TABLES, [])


async def test_a_session_saved_without_a_preset_gets_no_state_row(connection) -> None:
    claude = SessionId("s-claude-1")
    await SQLiteSessionStore(connection).save(_record(claude, "claude"))

    assert _rows(connection, "session_permission_state") == []
    assert await SQLitePermissionBindings(connection).state(claude) is None


async def test_a_database_from_the_release_before_migrates_with_no_state_rows(tmp_path) -> None:
    """A session launched with flags keeps its snapshot and gains no state row: nothing is
    backfilled, because the service never applied anything to it."""
    path = tmp_path / "state.sqlite3"
    older = tuple(m for m in MIGRATIONS if m[0] < STATE_MIGRATION)
    assert len(older) == STATE_MIGRATION - 1
    before = open_database(path, migrations=older)
    assert (
        before.execute(
            "SELECT name FROM sqlite_master WHERE name = 'session_permission_state'"
        ).fetchall()
        == []
    )
    # The older build's save: the session and its snapshot, and no state row.
    before.execute(
        "INSERT INTO sessions(session_id, project_id, profile_id, display_identity, state,"
        " created_at) VALUES ('s-old', 'p', 'codex', 'p/codex/regular/1', 'running',"
        " '2026-11-01T00:00:00+00:00')"
    )
    before.execute(
        "INSERT INTO session_permissions(session_id, preset) VALUES ('s-old', 'full_access')"
    )
    before.commit()
    before.close()

    after = open_database(path, migrations=MIGRATIONS)
    bindings = SQLitePermissionBindings(after)

    assert _rows(after, "session_permission_state") == []
    assert await bindings.state(SessionId("s-old")) is None
    assert await bindings.requested(SessionId("s-old")) is None
    assert await bindings.for_session(SessionId("s-old")) == PermissionPreset.FULL_ACCESS


async def test_an_applied_preset_records_the_daemon_and_clears_an_earlier_failure(
    connection,
) -> None:
    await SQLiteSessionStore(connection).save(_record(), permissions=PermissionPreset.ASK)
    bindings = SQLitePermissionBindings(connection)

    await bindings.mark_failed(SESSION, "timed out")
    assert await bindings.state(SESSION) == PermissionState(
        requested=PermissionPreset.ASK, applied=None, daemon=None, failure="timed out"
    )

    await bindings.mark_applied(SESSION, PermissionPreset.ASK, "boot-1:62581359")
    assert await bindings.state(SESSION) == PermissionState(
        requested=PermissionPreset.ASK,
        applied=PermissionPreset.ASK,
        daemon="boot-1:62581359",
        failure=None,
    )


async def test_a_failure_keeps_what_was_applied_before_it(connection) -> None:
    """A re-apply that fails after a daemon restart must not erase the record of the preset the
    pane last had: the owner is told about the failure, not shown a session that never had one."""
    await SQLiteSessionStore(connection).save(_record(), permissions=PermissionPreset.ASK)
    bindings = SQLitePermissionBindings(connection)
    await bindings.mark_applied(SESSION, PermissionPreset.ASK, "boot-1:1")

    await bindings.mark_failed(SESSION, "no Codex daemon")

    assert await bindings.state(SESSION) == PermissionState(
        requested=PermissionPreset.ASK,
        applied=PermissionPreset.ASK,
        daemon="boot-1:1",
        failure="no Codex daemon",
    )


async def test_a_new_request_replaces_the_requested_preset_and_clears_the_failure(
    connection,
) -> None:
    await SQLiteSessionStore(connection).save(_record(), permissions=PermissionPreset.ASK)
    bindings = SQLitePermissionBindings(connection)
    await bindings.mark_applied(SESSION, PermissionPreset.ASK, "boot-1:1")
    await bindings.mark_failed(SESSION, "timed out")

    await bindings.set_requested(SESSION, PermissionPreset.FULL_ACCESS)

    assert await bindings.requested(SESSION) == PermissionPreset.FULL_ACCESS
    assert await bindings.state(SESSION) == PermissionState(
        requested=PermissionPreset.FULL_ACCESS,
        applied=PermissionPreset.ASK,
        daemon="boot-1:1",
        failure=None,
    )
    # The launch snapshot is history, not the live request: it does not move.
    assert await bindings.for_session(SESSION) == PermissionPreset.ASK


async def test_every_write_stamps_the_moment_it_happened(connection) -> None:
    moments = iter(datetime(2026, 11, 12, 10, minute, tzinfo=UTC) for minute in (1, 2, 3))
    await SQLiteSessionStore(connection).save(_record(), permissions=PermissionPreset.ASK)
    bindings = SQLitePermissionBindings(connection, clock=lambda: next(moments))

    def stamp() -> str:
        return connection.execute("SELECT updated_at FROM session_permission_state").fetchone()[0]

    await bindings.mark_failed(SESSION, "timed out")
    assert stamp() == "2026-11-12T10:01:00+00:00"
    await bindings.mark_applied(SESSION, PermissionPreset.ASK, "boot-1:1")
    assert stamp() == "2026-11-12T10:02:00+00:00"
    await bindings.set_requested(SESSION, PermissionPreset.FULL_ACCESS)
    assert stamp() == "2026-11-12T10:03:00+00:00"


async def test_a_session_without_a_state_row_is_never_given_one(connection) -> None:
    """No row means the session was launched with flags by an older build. Recording an outcome
    for it would make it look like one the service manages, so nothing writes a row for it."""
    old = SessionId("s-old")
    await SQLiteSessionStore(connection).save(_record(old))
    bindings = SQLitePermissionBindings(connection)

    await bindings.mark_applied(old, PermissionPreset.ASK, "boot-1:1")
    await bindings.mark_failed(old, "timed out")
    with pytest.raises(LookupError):
        await bindings.set_requested(old, PermissionPreset.ASK)

    assert _rows(connection, "session_permission_state") == []
    assert await bindings.state(old) is None
    assert await bindings.requested(old) is None


@pytest.mark.parametrize("column", ["requested", "applied"])
async def test_the_table_refuses_a_preset_outside_the_closed_vocabulary(
    connection, column: str
) -> None:
    await SQLiteSessionStore(connection).save(_record(), permissions=PermissionPreset.ASK)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            f"UPDATE session_permission_state SET {column} = 'root'"  # noqa: S608 - a test constant
        )
