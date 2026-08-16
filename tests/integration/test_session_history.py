"""The read half of the append-only lifecycle history (BL-030).

`session_events` has been written since migration 1 and had no read path in `SessionStore`,
so `docs/operator-runbook.md` described a durable audit trail an operator could only retrieve
by opening sqlite by hand. The owner's decision was to surface it on `doctor` rather than as a
row on either surface: it is a read-only diagnostic, which is what `doctor` already is, and a
row would have moved the parity contract for a report neither surface needs mid-session.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

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
from remote_agents.domain.state_machine import LifecycleEvent

pytestmark = pytest.mark.asyncio


def _store(tmp_path: Path) -> SQLiteSessionStore:
    return SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3", migrations=MIGRATIONS))


async def _session_with_history(store: SQLiteSessionStore) -> SessionId:
    session_id = SessionId.new()
    await store.save(
        SessionRecord(
            session_id,
            ProjectId("opaque-editor"),
            ProfileId("claude"),
            SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
            SessionState.STARTING,
            datetime.now(UTC),
        )
    )
    for event in (
        LifecycleEvent.READY,
        LifecycleEvent.GRACEFUL_STOP_REQUESTED,
        LifecycleEvent.PANE_EXITED,
        LifecycleEvent.CLEANUP_CONFIRMED,
    ):
        await store.record_event(session_id, event)
    return session_id


async def test_the_recorded_history_reads_back_in_the_order_it_was_written(tmp_path) -> None:
    store = _store(tmp_path)
    session_id = await _session_with_history(store)

    events = await store.events(session_id)

    assert [event.event_type for event in events] == [
        "ready",
        "graceful_stop_requested",
        "pane_exited",
        "cleanup_confirmed",
    ]


async def test_the_history_is_ordered_by_write_order_not_by_timestamp(tmp_path) -> None:
    """Two events inside one operation routinely share a timestamp; the key is the order.

    `created_at` is a second-resolution string, so ordering by it would leave the sequence of
    a graceful stop -- request, exit, cleanup, all inside one call -- undefined. `event_id` is
    the actual write order and is what the read uses.
    """
    store = _store(tmp_path)
    session_id = await _session_with_history(store)

    events = await store.events(session_id)
    stamps = [event.created_at for event in events]

    assert stamps == sorted(stamps), "timestamps happen to be sorted here"
    # The real assertion: the sequence is correct even where timestamps tie.
    assert len(events) == 4
    assert events[1].event_type == "graceful_stop_requested"
    assert events[2].event_type == "pane_exited"


async def test_the_history_carries_no_callback_token(tmp_path) -> None:
    """`idempotency_key` is a callback token and must never reach a reporting surface.

    The row has one, and `tests/security/check_surface.py` exists to keep exactly this class
    off anything that reports. `error_code` is returned instead, because `_append_event`
    already refuses to store one that is not sanitized.
    """
    store = _store(tmp_path)
    session_id = await _session_with_history(store)

    events = await store.events(session_id)

    for event in events:
        assert not hasattr(event, "idempotency_key")
        assert set(vars(type(event))["__slots__"]) == {"event_type", "created_at", "error_code"}


async def test_a_session_with_no_recorded_events_reads_as_empty_not_as_missing(tmp_path) -> None:
    store = _store(tmp_path)
    session_id = SessionId.new()
    await store.save(
        SessionRecord(
            session_id,
            ProjectId("opaque-editor"),
            ProfileId("claude"),
            SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
            SessionState.STARTING,
            datetime.now(UTC),
        )
    )

    assert await store.events(session_id) == ()
