"""Why a session stopped must outlive the session, not just how it stopped."""

from datetime import UTC, datetime

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.state_machine import TERMINAL_STATES, LifecycleEvent


def _running(session_id: SessionId) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


def test_terminal_states_are_exactly_the_states_with_no_way_out() -> None:
    """Pin the derived set, so adding an escape transition cannot silently widen it."""
    assert TERMINAL_STATES == {SessionState.ENDED, SessionState.ORPHANED}


@pytest.mark.parametrize(
    ("event", "expected_state"),
    [
        (LifecycleEvent.RECONCILED_TERMINAL_MISSING, SessionState.ENDED),
        (LifecycleEvent.CLEANUP_CONFIRMED, SessionState.ENDED),
        (LifecycleEvent.VERIFIED_FORCE_STOP, SessionState.ENDED),
        (LifecycleEvent.AMBIGUOUS_TERMINAL_EVIDENCE, SessionState.ORPHANED),
    ],
)
async def test_a_session_that_stops_records_the_event_that_stopped_it(
    tmp_path, event: LifecycleEvent, expected_state: SessionState
) -> None:
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    session_id = SessionId.new()
    await store.save(_running(session_id))

    updated = await store.record_event(session_id, event)

    assert updated.state is expected_state
    assert updated.terminal_reason == event.value
    reloaded = await store.get(session_id)
    assert reloaded is not None
    assert reloaded.terminal_reason == event.value


async def test_an_oom_killed_session_is_distinguishable_from_one_the_owner_stopped(
    tmp_path,
) -> None:
    """The reason this whole column exists: both of these simply read ENDED."""
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    killed, stopped = SessionId.new(), SessionId.new()
    await store.save(_running(killed))
    await store.save(_running(stopped))

    await store.record_event(killed, LifecycleEvent.RECONCILED_TERMINAL_MISSING)
    await store.record_event(stopped, LifecycleEvent.CLEANUP_CONFIRMED)

    reasons = {record.session_id: record.terminal_reason for record in await store.list()}
    assert reasons[killed] == "reconciled_terminal_missing"
    assert reasons[stopped] == "cleanup_confirmed"


async def test_a_session_that_is_still_running_has_no_reason_yet(tmp_path) -> None:
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
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

    updated = await store.record_event(session_id, LifecycleEvent.READY)

    assert updated.state is SessionState.RUNNING
    assert updated.terminal_reason is None


async def test_recording_an_event_keeps_the_resume_identity_it_was_saved_with(tmp_path) -> None:
    """record_event rebuilds the projection, so every durable field has to survive it."""
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    session_id = SessionId.new()
    await store.save(
        SessionRecord(
            session_id,
            ProjectId("opaque-editor"),
            ProfileId("claude"),
            SessionDisplayIdentity("opaque-editor", "claude", "resumed", 1),
            SessionState.RUNNING,
            datetime.now(UTC),
            ProfileId("claude"),
            "source-123",
        )
    )

    updated = await store.record_event(session_id, LifecycleEvent.CLEANUP_CONFIRMED)

    assert updated.resume_profile_id == ProfileId("claude")
    assert updated.resume_source_id == "source-123"
    assert updated == await store.get(session_id)
