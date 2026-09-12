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

    assert current_version(connection) == len(MIGRATIONS)
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

    assert current_version(connection) == len(MIGRATIONS)
    assert path.with_suffix(".sqlite3.bak").exists()


def test_failed_migration_rolls_back_schema_version(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")

    # Derived from `MIGRATIONS`, not hand-numbered. This listed 1..11 with 11 as the broken
    # one until 2026-09-06, when a real migration 11 landed and silently disarmed it: the
    # database was already at 11, so `apply_migrations` skipped the broken entry
    # (`target <= version`), nothing raised, and the test failed on the raise it expected
    # rather than on what it is about. It would have gone on failing for every migration
    # after that, each time looking like a new problem. The next migration is one past
    # whatever exists.
    broken = len(MIGRATIONS) + 1
    with pytest.raises(sqlite3.OperationalError):
        open_database(
            tmp_path / "sessions.sqlite3",
            migrations=(
                *((version, "") for version in range(1, broken)),
                (broken, "CREATE TABLE broken ("),
            ),
        )

    assert current_version(connection) == len(MIGRATIONS)


def test_migration_eight_does_not_lose_the_resume_index_when_it_fails_partway(
    tmp_path: Path,
) -> None:
    """Migration 8 is the only one that *drops* an object before recreating it, so it is the
    only one where a partial application loses something. If its `DROP INDEX` committed and
    its `CREATE UNIQUE INDEX` did not, the database would sit at v7 with
    `sessions_resume_identity` **gone** — the index that stops two live sessions binding one
    conversation — and nothing would say so.

    `apply_migrations` wraps each migration in its own `BEGIN`/`commit`, so the two statements
    are atomic *together*. This proves that on the real statements rather than trusting it:
    the `CREATE` half is corrupted, and the index must survive the rollback intact.

    The sibling test above covers the neighbouring case and is deliberately different: there,
    the failure is in migration *9*, and version 8 correctly **stays applied**, because the
    unit of atomicity is one migration and not the whole run.

    **On `DROP INDEX IF EXISTS`, honestly:** since each migration is atomic, a retry always
    meets the index it is about to drop, so `IF EXISTS` fixes no reachable failure today. It
    is defence against a future edit that splits this migration or adds a statement between
    the two — the case atomicity currently rules out. It was carried as a residual reading
    "unreachable today, free to harden", and that is exactly what it remains.
    """
    path = tmp_path / "sessions.sqlite3"
    open_database(path, migrations=MIGRATIONS[:7]).close()
    assert current_version(sqlite3.connect(path)) == 7

    broken_eight = (8, MIGRATIONS[7][1].replace("ON sessions(", "ON nonexistent_table("))
    with pytest.raises(sqlite3.OperationalError):
        open_database(path, migrations=(*MIGRATIONS[:7], broken_eight))

    connection = sqlite3.connect(path)
    assert current_version(connection) == 7, "a failed migration must not advance the version"
    indexes = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "sessions_resume_identity" in indexes, (
        "migration 8's DROP committed while its CREATE did not, so the unique index that "
        "stops two live sessions binding one conversation is gone"
    )
    connection.close()

    # The retry then succeeds, reaching the real v8 with the partial index in place.
    open_database(path, migrations=MIGRATIONS).close()
    assert current_version(sqlite3.connect(path)) == len(MIGRATIONS)


def test_migration_five_adds_callback_state_tables_scoped_to_messages_not_clocks(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite3"
    open_database(path, migrations=MIGRATIONS[:4]).close()
    assert current_version(sqlite3.connect(path)) == 4

    connection = open_database(path)

    assert current_version(connection) == len(MIGRATIONS)
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

    record = (await service.launch(
        LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "launch-1")
    )).record

    assert record.state is SessionState.RUNNING
    assert await service.list_sessions() == (record,)


def test_migration_thirteen_renames_the_retired_profile_and_touches_nothing_else(
    tmp_path: Path,
) -> None:
    """Stored sessions must survive the retirement of the id they were launched under.

    Three rows, because the id lives in two columns and a rewrite of one is invisible to a
    test that only inspects the other: a RUNNING session on `claude-remote`, an ENDED one
    (history is queried, so it has to stay joinable), and one whose *resume* profile is the
    retired id while its own profile is not.

    The byte-identical assertion is the load-bearing half. An UPDATE with a wrong or missing
    WHERE clause rewrites rows nobody asked about -- a codex session becoming a Claude one --
    and the profile columns alone cannot show that. So every column of every row is compared
    before and after, and `display_identity` is deliberately among them: it carries the
    agent label this session was *launched* under, which is history and is not this
    migration's to rewrite.
    """
    path = tmp_path / "sessions.sqlite3"
    open_database(path, migrations=MIGRATIONS[:12]).close()
    assert current_version(sqlite3.connect(path)) == 12

    rows = (
        ("s-running", "proj-a", "claude-remote", "proj-a · claude-remote · regular · #1",
         "running", "2026-09-01T00:00:00+00:00", None, None, None),
        ("s-ended", "proj-a", "claude-remote", "proj-a · claude-remote · regular · #2",
         "ended", "2026-09-02T00:00:00+00:00", None, None, None),
        ("s-resumed", "proj-b", "codex", "proj-b · codex · resumed · #1",
         "running", "2026-09-03T00:00:00+00:00", None, "claude-remote", "conv-7"),
        ("s-untouched", "proj-b", "opencode", "proj-b · opencode · regular · #1",
         "running", "2026-09-04T00:00:00+00:00", None, None, None),
    )
    seed = sqlite3.connect(path)
    seed.executemany(
        "INSERT INTO sessions (session_id, project_id, profile_id, display_identity, state, "
        "created_at, terminal_reason, resume_profile_id, resume_source_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    seed.commit()
    before = {
        row[0]: row
        for row in seed.execute(
            "SELECT session_id, project_id, profile_id, display_identity, state, created_at, "
            "terminal_reason, resume_profile_id, resume_source_id FROM sessions"
        )
    }
    seed.close()

    connection = open_database(path, migrations=MIGRATIONS)

    assert current_version(connection) == len(MIGRATIONS)
    after = {
        row[0]: row
        for row in connection.execute(
            "SELECT session_id, project_id, profile_id, display_identity, state, created_at, "
            "terminal_reason, resume_profile_id, resume_source_id FROM sessions"
        )
    }
    connection.close()

    assert after["s-running"][2] == "claude"
    assert after["s-ended"][2] == "claude", "history is queried; an ended row must migrate too"
    assert after["s-resumed"][7] == "claude", "the retired id lives in the resume column as well"
    assert after["s-resumed"][2] == "codex", "a row's own profile is not the resume profile"
    assert set(after) == set(before), "no row was added or lost"
    for session_id, row in after.items():
        expected = tuple(
            "claude" if index in {2, 7} and value == "claude-remote" else value
            for index, value in enumerate(before[session_id])
        )
        assert row == expected, (
            f"{session_id} changed in a column this migration does not own -- an UPDATE "
            "without a WHERE clause rewrites rows nobody asked about"
        )


def test_migration_thirteen_survives_one_conversation_resumed_under_both_ids(
    tmp_path: Path,
) -> None:
    """**This migration's first form took the service down, and this is the case that did it.**

    `sessions_resume_identity` is UNIQUE on `(resume_profile_id, resume_source_id)` for every
    row that is not ended (migration 8). While both ids existed, resuming one Claude
    conversation under each of them produced `('claude', X)` and `('claude-remote', X)` --
    two distinct keys, so the index accepted both, and nothing serialised them because
    `SessionService._resume_locked` takes its lock per *(profile, conversation)*.

    Renaming the second row then makes both keys `('claude', X)`. Measured on this exact
    fixture: `IntegrityError: UNIQUE constraint failed`, the whole migration rolled back, the
    schema left at 12 -- and since `open_database` migrates on every start, the service would
    have failed to start on that host and kept failing.

    The resolution keeps both sessions. The row under the **retired** id loses its resume
    *binding*, not its row and not its state: the id that still exists keeps the conversation,
    and nothing is deleted. That is also what the index has always meant -- two live panes on
    one conversation are impossible -- applied to data the old model let in because it counted
    the two ids as different agents.
    """
    path = tmp_path / "sessions.sqlite3"
    open_database(path, migrations=MIGRATIONS[:12]).close()
    seed = sqlite3.connect(path)
    seed.executemany(
        "INSERT INTO sessions (session_id, project_id, profile_id, display_identity, state, "
        "created_at, resume_profile_id, resume_source_id) VALUES (?,?,?,?,?,?,?,?)",
        (
            ("s-keeps", "p", "claude", "p · claude · resumed · #1", "running",
             "2026-09-01T00:00:00+00:00", "claude", "conv-X"),
            ("s-loses", "p", "claude-remote", "p · claude-remote · resumed · #2", "running",
             "2026-09-01T00:00:01+00:00", "claude-remote", "conv-X"),
            # An *ended* pair on one conversation must survive untouched: ended rows are
            # outside the partial index, so they cannot collide and must keep their history.
            ("s-ended-a", "p", "claude", "p · claude · resumed · #3", "ended",
             "2026-09-01T00:00:02+00:00", "claude", "conv-Y"),
            ("s-ended-b", "p", "claude-remote", "p · claude-remote · resumed · #4", "ended",
             "2026-09-01T00:00:03+00:00", "claude-remote", "conv-Y"),
        ),
    )
    seed.commit()
    seed.close()

    connection = open_database(path, migrations=MIGRATIONS)

    assert current_version(connection) == len(MIGRATIONS), (
        "the migration rolled back, so every start of the service re-runs and re-fails it"
    )
    rows = {
        row[0]: row
        for row in connection.execute(
            "SELECT session_id, profile_id, state, resume_profile_id, resume_source_id "
            "FROM sessions"
        )
    }
    connection.close()

    assert set(rows) == {"s-keeps", "s-loses", "s-ended-a", "s-ended-b"}, "no session was lost"
    assert rows["s-keeps"][3:] == ("claude", "conv-X"), "the surviving id keeps the binding"
    assert rows["s-loses"][3:] == (None, None), (
        "the retired id's row must lose its binding rather than its session"
    )
    assert rows["s-loses"][1:3] == ("claude", "running"), (
        "the losing row is still a live claude session -- only its resume provenance went"
    )
    # Ended rows are outside the index, so both keep the conversation they were resumed from.
    assert rows["s-ended-a"][3:] == ("claude", "conv-Y")
    assert rows["s-ended-b"][3:] == ("claude", "conv-Y")
