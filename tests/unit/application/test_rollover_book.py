"""What both surfaces call for rollovers (DEC-046, DEC-115): a request is a row and nothing
more, and a cancel withdraws only a request the workflow has not answered."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.application.rollover_book import RolloverBook
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.rollover import RolloverState

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
PROJECT = ProjectId("remote-agents")
CLAUDE = ProfileId("claude")
HANDOFF = "h-0123456789abcdef0123"


@pytest.fixture
def store(tmp_path: Path) -> SQLiteRolloverStore:
    return SQLiteRolloverStore(open_database(tmp_path / "sessions.sqlite3"))


def _book(store: SQLiteRolloverStore) -> RolloverBook:
    return RolloverBook(store, now=lambda: NOW)


async def test_a_request_is_one_requested_row_and_a_second_is_refused(
    store: SQLiteRolloverStore,
) -> None:
    book, session = _book(store), SessionId.new()

    first = await book.request(session, project_id=PROJECT, profile_id=CLAUDE)
    second = await book.request(session, project_id=PROJECT, profile_id=CLAUDE)

    assert first is not None and first.state is RolloverState.REQUESTED
    assert second is None
    assert await book.open_for(session) == first
    assert await book.open_rollovers() == (first,)


async def test_a_cancel_withdraws_a_request(store: SQLiteRolloverStore) -> None:
    book, session = _book(store), SessionId.new()
    requested = await book.request(session, project_id=PROJECT, profile_id=CLAUDE)
    assert requested is not None

    assert await book.cancel(session) is True

    assert (await store.get(requested.id)).state is RolloverState.CANCELLED
    assert await book.open_for(session) is None
    assert await book.cancel(session) is False


async def test_a_cancel_does_not_touch_a_rollover_the_workflow_answered(
    store: SQLiteRolloverStore,
) -> None:
    book, session = _book(store), SessionId.new()
    opened = await store.open_for_ready(
        session, HANDOFF, project_id=PROJECT, profile_id=CLAUDE, plan=None, at=NOW
    )
    assert opened is not None

    assert await book.cancel(session) is False

    assert (await store.get(opened.id)).state is RolloverState.HANDOFF_READY


async def test_lineage_reads_through_to_completed_rollovers(store: SQLiteRolloverStore) -> None:
    book, predecessor, successor = _book(store), SessionId.new(), SessionId.new()
    opened = await store.open_for_ready(
        predecessor, HANDOFF, project_id=PROJECT, profile_id=CLAUDE, plan=None, at=NOW
    )
    assert opened is not None
    await store.advance(opened.id, RolloverState.SUCCESSOR_STARTING, at=NOW)
    await store.record_successor(opened.id, successor, at=NOW)
    for state in (
        RolloverState.ADOPTING,
        RolloverState.SUCCESSOR_ACCEPTED,
        RolloverState.PREDECESSOR_STOPPING,
        RolloverState.COMPLETED,
    ):
        await store.advance(opened.id, state, at=NOW)

    assert await book.continued_as(predecessor) == successor
    assert await book.continued_from(successor) == predecessor
