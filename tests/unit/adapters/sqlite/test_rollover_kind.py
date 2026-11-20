"""A rollover has a kind: `workflow` (a plan's handoff) or `note` (the owner's plan-less press).

Migration 27 adds the column with a default, so every row a v0.70.x store holds reads
`workflow` after the upgrade and no row is lost or changed (Sub-plan 5 Task 2.1, OD-6).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS, apply_migrations
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.rollover import NEEDS_SUCCESSOR, RolloverKind, RolloverState

_STAMP = "2026-11-20T10:00:00.000000+00:00"
_V0703 = 26
"""The schema a v0.70.3 to v0.70.5 store is at: the last migration before the kind column."""


def _old_store_with_every_state(path) -> tuple[sqlite3.Connection, dict[str, RolloverState]]:
    connection = open_database(path, migrations=MIGRATIONS[:_V0703])
    ids: dict[str, RolloverState] = {}
    for index, state in enumerate(RolloverState):
        rollover_id = f"{index:032x}"
        successor = str(SessionId.new()) if state in NEEDS_SUCCESSOR else None
        connection.execute(
            "INSERT INTO rollovers(rollover_id, handoff_id, predecessor_session_id,"
            " successor_session_id, project_id, profile_id, reason, plan, state, failure_code,"
            " failure_detail, requested_at, updated_at, source_root, target_json)"
            " VALUES (?, ?, ?, ?, 'orchard', 'claude', 'workflow', NULL, ?, NULL, NULL, ?, ?,"
            " NULL, NULL)",
            (
                rollover_id,
                f"h-{index:020x}",
                str(SessionId.new()),
                successor,
                state.value,
                _STAMP,
                _STAMP,
            ),
        )
        connection.execute(
            "INSERT INTO rollover_events(rollover_id, from_state, to_state, failure_code,"
            " detail, created_at) VALUES (?, NULL, ?, NULL, NULL, ?)",
            (rollover_id, state.value, _STAMP),
        )
        ids[rollover_id] = state
    connection.commit()
    return connection, ids


async def test_an_old_store_reads_every_rollover_as_workflow_after_the_migration(tmp_path) -> None:
    connection, ids = _old_store_with_every_state(tmp_path / "old.sqlite3")
    before = [tuple(r) for r in connection.execute("SELECT rowid, * FROM rollovers ORDER BY rowid")]
    events = [tuple(r) for r in connection.execute("SELECT * FROM rollover_events ORDER BY rowid")]
    assert len(before) == len(RolloverState), "the premise: one row in every state"

    apply_migrations(connection)

    after = [tuple(r) for r in connection.execute("SELECT rowid, * FROM rollovers ORDER BY rowid")]
    assert [row[:-1] for row in after] == before, "a row changed or was lost"
    assert {row[-1] for row in after} == {"workflow"}
    assert [
        tuple(r) for r in connection.execute("SELECT * FROM rollover_events ORDER BY rowid")
    ] == (events)
    store = SQLiteRolloverStore(connection)
    for rollover_id, state in ids.items():
        row = await store.get(rollover_id)
        assert row is not None and row.state is state
        assert row.kind is RolloverKind.WORKFLOW
    connection.close()


def test_the_table_refuses_a_kind_it_does_not_know(tmp_path) -> None:
    connection = open_database(tmp_path / "new.sqlite3")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        connection.execute(
            "INSERT INTO rollovers(rollover_id, predecessor_session_id, project_id, profile_id,"
            " reason, state, requested_at, updated_at, kind)"
            " VALUES ('x', ?, 'orchard', 'claude', 'owner', 'requested', ?, ?, 'other')",
            (str(SessionId.new()), _STAMP, _STAMP),
        )
    connection.close()


async def test_a_new_request_is_a_workflow_rollover_and_a_note_row_reads_back_as_one(
    tmp_path,
) -> None:
    connection = open_database(tmp_path / "new.sqlite3")
    store = SQLiteRolloverStore(connection)
    requested = await store.request(
        SessionId.new(),
        project_id=ProjectId("orchard"),
        profile_id=ProfileId("claude"),
        at=datetime.now(UTC),
    )
    assert requested is not None and requested.kind is RolloverKind.WORKFLOW
    connection.execute("UPDATE rollovers SET kind = 'note' WHERE rollover_id = ?", (requested.id,))
    connection.commit()
    reread = await store.get(requested.id)
    assert reread is not None and reread.kind is RolloverKind.NOTE
    assert (await store.latest_for_each([requested.predecessor_session_id]))[
        requested.predecessor_session_id
    ].kind is RolloverKind.NOTE
    connection.close()


async def test_a_note_request_is_a_note_row_with_a_path_safe_id(tmp_path) -> None:
    connection = open_database(tmp_path / "new.sqlite3")
    store = SQLiteRolloverStore(connection)
    predecessor = SessionId.new()
    asked = await store.request(
        predecessor,
        project_id=ProjectId("orchard"),
        profile_id=ProfileId("claude"),
        at=datetime.now(UTC),
        kind=RolloverKind.NOTE,
    )
    assert asked is not None and asked.kind is RolloverKind.NOTE
    assert asked.state is RolloverState.REQUESTED and asked.reason == "owner"
    assert re.fullmatch(r"n-[0-9a-f]{20}", asked.id), asked.id
    again = await store.request(
        predecessor,
        project_id=ProjectId("orchard"),
        profile_id=ProfileId("claude"),
        at=datetime.now(UTC),
        kind=RolloverKind.NOTE,
    )
    assert again is None, "one open rollover per session, whatever its kind"
    connection.close()


async def test_a_ready_envelope_never_turns_a_note_request_into_a_workflow_handoff(
    tmp_path,
) -> None:
    """A stray `ready` for a session the owner asked a note rollover of is refused: the note
    row stays REQUESTED with no handoff, and no second row opens."""
    connection = open_database(tmp_path / "new.sqlite3")
    store = SQLiteRolloverStore(connection)
    predecessor = SessionId.new()
    asked = await store.request(
        predecessor,
        project_id=ProjectId("orchard"),
        profile_id=ProfileId("claude"),
        at=datetime.now(UTC),
        kind=RolloverKind.NOTE,
    )
    assert asked is not None
    opened = await store.open_for_ready(
        predecessor,
        "h-" + "a" * 20,
        project_id=ProjectId("orchard"),
        profile_id=ProfileId("claude"),
        plan="/abs/plan.md",
        at=datetime.now(UTC),
    )
    assert opened is None
    row = await store.get(asked.id)
    assert row is not None and row.state is RolloverState.REQUESTED and row.handoff_id is None
    assert connection.execute("SELECT COUNT(*) FROM rollovers").fetchone()[0] == 1
    connection.close()
