import sqlite3
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
from remote_agents.domain.state_machine import LifecycleEvent


async def test_resume_identity_is_durable_and_unique(tmp_path) -> None:
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    record = SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "resumed", 1),
        SessionState.STARTING,
        datetime.now(UTC),
        ProfileId("claude"),
        "source-123",
    )
    await store.save(record)

    assert await store.get_by_resume_source(ProfileId("claude"), "source-123") == record


def _bound(session_id: SessionId, state: SessionState, sequence: int) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "resumed", sequence),
        state,
        datetime.now(UTC),
        ProfileId("claude"),
        "source-123",
    )


async def test_resume_identity_stays_exclusive_while_its_session_is_not_ended(tmp_path) -> None:
    """Two live panes on one conversation is the thing the unique index exists to stop, and
    that has not changed. Only ENDED releases it — `state_machine.TERMINAL_STATES` is exactly
    `{ENDED}`, so this rule is the domain's rather than a hand-written list beside it."""
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    live = SessionId.new()
    await store.save(_bound(live, SessionState.RUNNING, 1))

    found = await store.get_by_resume_source(ProfileId("claude"), "source-123")

    assert found is not None and found.session_id == live
    with pytest.raises(sqlite3.IntegrityError):
        await store.save(_bound(SessionId.new(), SessionState.STARTING, 2))


async def test_a_conversation_binds_again_once_its_session_has_ended(tmp_path) -> None:
    """The Stage 3 Critical, fixed at its root: without this, one mis-tap attached a
    conversation to a session permanently, because nothing deletes a session row."""
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    first = SessionId.new()
    await store.save(_bound(first, SessionState.RUNNING, 1))
    await store.record_event(first, LifecycleEvent.GRACEFUL_STOP_REQUESTED)
    await store.record_event(first, LifecycleEvent.PANE_EXITED)
    await store.record_event(first, LifecycleEvent.CLEANUP_CONFIRMED)
    assert (await store.get(first)).state is SessionState.ENDED

    assert await store.get_by_resume_source(ProfileId("claude"), "source-123") is None

    second = SessionId.new()
    await store.save(_bound(second, SessionState.STARTING, 2))
    found = await store.get_by_resume_source(ProfileId("claude"), "source-123")
    assert found is not None and found.session_id == second


async def test_an_ended_record_keeps_its_resume_identity_for_the_audit_trail(tmp_path) -> None:
    """Releasing exclusivity must not erase history. The ended record still names the
    conversation it resumed — which is what lets the audit answer *what was resumed*."""
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    first = SessionId.new()
    await store.save(_bound(first, SessionState.RUNNING, 1))
    await store.record_event(first, LifecycleEvent.GRACEFUL_STOP_REQUESTED)
    await store.record_event(first, LifecycleEvent.PANE_EXITED)
    await store.record_event(first, LifecycleEvent.CLEANUP_CONFIRMED)
    await store.save(_bound(SessionId.new(), SessionState.STARTING, 2))

    ended = await store.get(first)

    assert ended is not None
    assert ended.resume_source_id == "source-123"
    assert ended.resume_profile_id == ProfileId("claude")
    assert len(await store.list()) == 2, "history is kept, not replaced"
