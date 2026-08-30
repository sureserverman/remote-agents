"""Agent activity becomes durable: an append-only record of observations, for the feed.

This records *observations* — what an agent was seen doing — never delivery state: the
Telegram notifier's queue and rate windows stay in memory by decision (DEC-026), and this
table is the local feed's source, not a second delivery ledger. Its standing messages moved
out of memory in migration 10, which is a different question — *which message is this
session's notification* — and has its own table. The reader is bounded and newest-first
because a feed is a glance, not an archive.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind, AgentActivity


@pytest.fixture
def store(tmp_path: Path):
    connection = open_database(tmp_path / "state.sqlite3", migrations=MIGRATIONS)
    yield SQLiteActivityStore(connection)
    connection.close()


def _activity(kind: ActivityKind, *, minutes_ago: int = 0, detail: str | None = None):
    return AgentActivity(
        "01234567-89ab-cdef-0123-456789abcdef",
        kind,
        detail,
        datetime.now(UTC) - timedelta(minutes=minutes_ago),
        ActivityConfidence.REPORTED,
    )


async def test_the_migration_applies_over_an_existing_database(tmp_path: Path) -> None:
    """An operator's live database migrates forward; the new table starts empty."""
    path = tmp_path / "state.sqlite3"
    open_database(path, migrations=MIGRATIONS[:-1]).close()
    connection = open_database(path, migrations=MIGRATIONS)
    try:
        rows = connection.execute("SELECT COUNT(*) FROM agent_activity").fetchone()
        assert rows == (0,)
    finally:
        connection.close()


async def test_appended_observations_read_back_newest_first_and_bounded(store) -> None:
    await store.append(_activity(ActivityKind.COMPLETED, minutes_ago=3))
    await store.append(_activity(ActivityKind.NEEDS_ANSWER, minutes_ago=1, detail="push?"))
    await store.append(_activity(ActivityKind.LIMIT_REACHED, minutes_ago=0))

    recent = await store.recent(limit=2)
    assert [activity.kind for activity in recent] == [
        ActivityKind.LIMIT_REACHED,
        ActivityKind.NEEDS_ANSWER,
    ]
    # The agent's words persist deliberately (DEC-037, superseding DEC-013's storage
    # clause on the owner's decision): the question is most of the feed's news.
    assert recent[1].detail == "push?"
    assert all(activity.observed_at.tzinfo is not None for activity in recent)


async def test_the_round_trip_preserves_every_field(store) -> None:
    original = AgentActivity(
        "01234567-89ab-cdef-0123-456789abcdef",
        ActivityKind.LIMIT_REACHED,
        None,
        datetime.now(UTC),
        ActivityConfidence.INFERRED,
    )
    await store.append(original)
    (loaded,) = await store.recent(limit=10)
    assert loaded.session_id == original.session_id
    assert loaded.kind is original.kind
    assert loaded.detail is None
    assert loaded.confidence is ActivityConfidence.INFERRED


async def test_the_reader_refuses_a_nonpositive_bound(store) -> None:
    with pytest.raises(ValueError):
        await store.recent(limit=0)


async def test_a_poisoned_row_costs_itself_never_the_whole_glance(store, tmp_path: Path) -> None:
    """Retiring an ActivityKind is an expected evolution (its own docstring says so); a
    row written under the old vocabulary is skipped with a log line, and every row this
    build still speaks renders."""
    await store.append(_activity(ActivityKind.COMPLETED))
    store._connection.execute(
        "INSERT INTO agent_activity(session_id, kind, detail, confidence, observed_at)"
        " VALUES ('s', 'retired-kind', NULL, 'reported', '2026-08-19T00:00:00+00:00')"
    )
    store._connection.commit()
    recent = await store.recent(limit=10)
    assert [activity.kind for activity in recent] == [ActivityKind.COMPLETED]


def test_the_migration_count_is_pinned_by_hand() -> None:
    """`len(MIGRATIONS)` comparisons elsewhere are conveniences; this literal is the one
    assertion an accidentally dropped migration cannot pass. Bump this — and only this —
    when adding a migration."""
    assert len(MIGRATIONS) == 10
