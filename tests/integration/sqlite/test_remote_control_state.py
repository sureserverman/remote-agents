"""The last observed Remote Control state has to survive the round trip, and the rebuilds.

Both surfaces offered Enable *and* Disable together because nothing knew which state a pane
was in: `set_remote_control` returned a `RemoteControlState` and nobody stored it. Half of
that pair was always a no-op, on the deepest screen in the bot.

The rebuild cases are here for the reason `test_orphan_provenance.py` states: `record_event`
and `set_label` reconstruct `SessionRecord` **positionally**, so a field appended after
`orphan_provenance` is exactly the shape those two silently drop. A dropped state does not
raise — it reads back as unknown, which downgrades the detail screen to offering both
buttons again, with nothing failing.
"""

import sqlite3
from datetime import UTC, datetime

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.domain.state_machine import LifecycleEvent


def _record(session_id: SessionId) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


async def test_a_new_record_has_no_remote_control_state_until_one_is_observed(tmp_path) -> None:
    """Unknown is the honest answer for a session nobody has toggled, and it is the answer
    that makes the surfaces offer both buttons — which is what they did before this existed."""
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    session_id = SessionId.new()
    await store.save(_record(session_id))

    stored = await store.get(session_id)

    assert stored is not None
    assert stored.remote_control_state is None


async def test_an_observed_remote_control_state_survives_a_reload(tmp_path) -> None:
    database = tmp_path / "sessions.sqlite3"
    store = SQLiteSessionStore(open_database(database))
    session_id = SessionId.new()
    await store.save(_record(session_id))

    updated = await store.set_remote_control_state(session_id, RemoteControlState.ACTIVE)

    assert updated.remote_control_state is RemoteControlState.ACTIVE
    reopened = SQLiteSessionStore(open_database(database))
    reloaded = await reopened.get(session_id)
    assert reloaded is not None
    assert reloaded.remote_control_state is RemoteControlState.ACTIVE


async def test_an_unknown_result_clears_rather_than_records_a_guess(tmp_path) -> None:
    """UNKNOWN means the toggle could not tell. Storing it as a *state* would let a surface
    offer the opposite action on the strength of something nobody observed."""
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    session_id = SessionId.new()
    await store.save(_record(session_id))
    await store.set_remote_control_state(session_id, RemoteControlState.ACTIVE)

    cleared = await store.set_remote_control_state(session_id, RemoteControlState.UNKNOWN)

    assert cleared.remote_control_state is None
    reloaded = await store.get(session_id)
    assert reloaded is not None and reloaded.remote_control_state is None


async def test_the_state_survives_the_two_rebuilds_that_drop_appended_fields(tmp_path) -> None:
    """`record_event` and `set_label` rebuild the record positionally. This is the case
    `test_orphan_provenance.py` was written for, one field later."""
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    session_id = SessionId.new()
    await store.save(_record(session_id))
    await store.set_remote_control_state(session_id, RemoteControlState.ACTIVE)

    renamed = await store.set_label(session_id, "named")
    assert renamed.remote_control_state is RemoteControlState.ACTIVE

    after_event = await store.record_event(session_id, LifecycleEvent.GRACEFUL_STOP_REQUESTED)
    assert after_event.remote_control_state is RemoteControlState.ACTIVE

    reloaded = await store.get(session_id)
    assert reloaded is not None and reloaded.remote_control_state is RemoteControlState.ACTIVE


async def test_a_row_written_before_the_migration_reads_back_as_unknown(tmp_path) -> None:
    """No backfill, for DEC-020's reason one column over: an unmigrated row and a
    never-toggled session are both genuinely unknown, and guessing either way would offer an
    action on the strength of a guess."""
    database = tmp_path / "sessions.sqlite3"
    connection = sqlite3.connect(database)
    with connection:
        for version, statements in MIGRATIONS:
            if version > 6:
                break
            if statements:
                connection.executescript(statements)
        connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        connection.execute("DELETE FROM schema_version")
        connection.execute("INSERT INTO schema_version(version) VALUES (6)")
        connection.execute(
            """
            INSERT INTO sessions(
                session_id, project_id, profile_id, display_identity, state, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "00000000-0000-0000-0000-0000000000ff",
                "opaque-editor",
                "claude",
                "opaque-editor · claude · regular · #1",
                SessionState.RUNNING.value,
                datetime.now(UTC).isoformat(),
            ),
        )
    connection.close()

    store = SQLiteSessionStore(open_database(database))
    reloaded = await store.get(SessionId.parse("00000000-0000-0000-0000-0000000000ff"))

    assert reloaded is not None
    assert reloaded.remote_control_state is None


def test_the_migration_is_idempotent_when_the_database_is_reopened(tmp_path) -> None:
    """Reopening runs the migration list again; `current_version` is what stops it repeating
    an ALTER that SQLite would refuse the second time."""
    database = tmp_path / "sessions.sqlite3"
    open_database(database).close()

    reopened = open_database(database)

    columns = {row[1] for row in reopened.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "remote_control_state" in columns
