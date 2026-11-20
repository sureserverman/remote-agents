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


async def test_a_cancel_that_loses_the_race_to_the_workflow_withdraws_nothing(
    store: SQLiteRolloverStore,
) -> None:
    """The book read REQUESTED; before its write, the pass (another process) answered it."""
    book, session = _book(store), SessionId.new()
    requested = await book.request(session, project_id=PROJECT, profile_id=CLAUDE)
    assert requested is not None
    real_open_for = book.open_for

    async def read_then_lose_the_race(session_id: SessionId):
        seen = await real_open_for(session_id)
        await store.open_for_ready(
            session, HANDOFF, project_id=PROJECT, profile_id=CLAUDE, plan=None, at=NOW
        )
        return seen

    book.open_for = read_then_lose_the_race  # type: ignore[method-assign]

    assert await book.cancel(session) is False
    assert (await store.get(requested.id)).state is RolloverState.HANDOFF_READY


# --- Sub-plan 5 Task 1.4: every listed row's rollover reads at once ----------------------------


async def test_marks_for_answers_latest_events_and_lineage_for_many_sessions(
    store: SQLiteRolloverStore,
) -> None:
    from datetime import timedelta

    at = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
    first, second, third, quiet = (SessionId.new() for _ in range(4))
    opened = await store.open_for_ready(
        first,
        "h-00000000000000000001",
        project_id=ProjectId("p"),
        profile_id=ProfileId("claude"),
        plan=None,
        at=at,
    )
    assert opened is not None
    for offset, state in enumerate(
        (
            RolloverState.SUCCESSOR_STARTING,
            RolloverState.ADOPTING,
            RolloverState.SUCCESSOR_ACCEPTED,
            RolloverState.PREDECESSOR_STOPPING,
            RolloverState.COMPLETED,
        ),
        start=1,
    ):
        await store.advance(
            opened.id,
            state,
            at=at + timedelta(seconds=offset),
            successor_session_id=second if state is RolloverState.SUCCESSOR_STARTING else None,
        )
    requested = await store.request(
        third, project_id=ProjectId("p"), profile_id=ProfileId("claude"), at=at
    )
    await store.note(requested.id, "waiting: boundary", at=at + timedelta(seconds=1))

    marks = await RolloverBook(store).marks_for((first, second, third, quiet))

    assert marks.latest[first].state is RolloverState.COMPLETED
    assert marks.latest[third].state is RolloverState.REQUESTED
    assert second not in marks.latest and quiet not in marks.latest
    assert [event.detail for event in marks.events[requested.id]][-1] == "waiting: boundary"
    assert [event.to_state for event in marks.events[opened.id]][-1] is RolloverState.COMPLETED
    assert marks.continued_as == {first: second}
    assert marks.continued_from == {second: first}


async def test_marks_for_no_sessions_reads_nothing(store: SQLiteRolloverStore) -> None:
    marks = await RolloverBook(store).marks_for(())

    assert (marks.latest, marks.events, marks.continued_from, marks.continued_as) == (
        {},
        {},
        {},
        {},
    )


# Rollover with a note (BL-119, OD-2) -------------------------------------------------------


def _session(profile: str = "claude", state=None):
    from dataclasses import replace

    from remote_agents.domain.models import SessionDisplayIdentity, SessionRecord, SessionState

    record = SessionRecord(
        session_id=SessionId.new(),
        project_id=PROJECT,
        profile_id=ProfileId(profile),
        display=SessionDisplayIdentity("probe", profile, "regular", 1),
        state=SessionState.RUNNING,
        created_at=NOW,
    )
    return record if state is None else replace(record, state=state)


class _Switch:
    def __init__(self, on: bool) -> None:
        self._on = on

    async def read(self) -> bool:
        return self._on


def _note_book(store: SQLiteRolloverStore) -> RolloverBook:
    return RolloverBook(
        store,
        rollable=frozenset({CLAUDE}),
        note_rollable=frozenset({CLAUDE, ProfileId("codex")}),
        now=lambda: NOW,
    )


@pytest.mark.parametrize("switch_on", [True, False])
async def test_a_note_press_writes_one_requested_note_row_and_a_second_writes_none(
    store: SQLiteRolloverStore, switch_on: bool
) -> None:
    from remote_agents.application.session_actions import NOTE_ROLLOVER, rollover_outcome
    from remote_agents.domain.rollover import RolloverKind

    book, record = _note_book(store), _session()
    assert NOTE_ROLLOVER in await book.offered(record, _Switch(switch_on))

    said = await book.press(NOTE_ROLLOVER, record, _Switch(switch_on))
    assert said == rollover_outcome(NOTE_ROLLOVER, done=True)
    opened = await book.open_for(record.session_id)
    assert opened is not None
    assert opened.kind is RolloverKind.NOTE and opened.state is RolloverState.REQUESTED

    said = await book.press(NOTE_ROLLOVER, record, _Switch(switch_on))
    assert said == rollover_outcome(NOTE_ROLLOVER, done=False)
    assert await book.open_rollovers() == (opened,)


async def test_a_stale_note_press_on_a_profile_that_declares_none_asks_for_nothing(
    store: SQLiteRolloverStore,
) -> None:
    from remote_agents.application.session_actions import (
        NOTE_ROLLOVER,
        NOTE_ROLLOVER_UNAVAILABLE,
    )

    book, record = _note_book(store), _session("opencode")
    assert NOTE_ROLLOVER not in await book.offered(record, _Switch(True))
    assert await book.press(NOTE_ROLLOVER, record, _Switch(True)) == NOTE_ROLLOVER_UNAVAILABLE
    assert await book.open_rollovers() == ()


async def test_a_note_request_is_cancelled_like_any_request(store: SQLiteRolloverStore) -> None:
    from remote_agents.application.session_actions import CANCEL_ROLLOVER, NOTE_ROLLOVER

    book, record = _note_book(store), _session()
    await book.press(NOTE_ROLLOVER, record, None)
    assert await book.offered(record, None) == (CANCEL_ROLLOVER,)
    assert await book.cancel(record.session_id) is True
    assert await book.open_for(record.session_id) is None
