"""Restart reconciliation against an isolated tmux server and real SQLite projection."""

from datetime import UTC, datetime, timedelta

from test_terminal_launch import STARTUP_BUDGET, make_terminal

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.application.reconcile import ReconciliationService
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


def starting_record(session_id: SessionId) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque-editor"),
        ProfileId("fake"),
        SessionDisplayIdentity("opaque-editor", "fake", "regular", 1),
        SessionState.STARTING,
        datetime.now(UTC),
    )


async def test_restart_reconciliation_recovers_preserves_and_ends_a_session(tmp_path):
    terminal, gateway = make_terminal(tmp_path, timeout=STARTUP_BUDGET)
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    reconciler = ReconciliationService(store, settle_after=timedelta(0))
    session_id = SessionId.new()
    try:
        await store.save(starting_record(session_id))
        assert (await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))).live

        first = await reconciler.reconcile(await terminal.managed_observations())
        assert first[0].state is SessionState.RUNNING
        assert (await store.get(session_id)).state is SessionState.RUNNING
        assert _event_count(store) == 1

        await reconciler.reconcile(await terminal.managed_observations())
        assert _event_count(store) == 1

        assert (await terminal.graceful_stop(session_id, ProfileId("fake"))).preserved
        preserved = await reconciler.reconcile(await terminal.managed_observations())
        assert preserved[0].state is SessionState.PRESERVED
        assert (await store.get(session_id)).state is SessionState.PRESERVED

        await terminal.cleanup(session_id)
        vanished = await reconciler.reconcile(await terminal.managed_observations())
        assert vanished[0].state is SessionState.ENDED
        assert (await store.get(session_id)).state is SessionState.ENDED
    finally:
        try:
            await gateway.mutate("kill-session", f"ra-{session_id}")
        except RuntimeError:
            pass


async def test_reconciliation_quarantines_a_trusted_live_session_without_a_database_row(tmp_path):
    terminal, gateway = make_terminal(tmp_path, timeout=STARTUP_BUDGET)
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    reconciler = ReconciliationService(store, settle_after=timedelta(0))
    session_id = SessionId.new()
    try:
        assert (await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))).live

        result = await reconciler.reconcile(await terminal.managed_observations())

        assert result == (result[0],)
        assert result[0].state is SessionState.ORPHANED
        persisted = await store.get(session_id)
        assert persisted is not None
        assert persisted.state is SessionState.ORPHANED
        assert persisted.project_id == ProjectId("opaque-editor")
        assert persisted.profile_id == ProfileId("fake")
    finally:
        try:
            await gateway.mutate("kill-session", f"ra-{session_id}")
        except RuntimeError:
            pass


async def test_reconciliation_marks_a_crash_before_launch_commit_as_failed(tmp_path):
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    reconciler = ReconciliationService(store, settle_after=timedelta(0))
    session_id = SessionId.new()
    await store.save(starting_record(session_id))

    result = await reconciler.reconcile(())

    assert result == (result[0],)
    assert result[0].state is SessionState.FAILED
    assert (await store.get(session_id)).state is SessionState.FAILED


async def test_trusted_tmux_inspection_is_available_without_a_database(tmp_path):
    terminal, gateway = make_terminal(tmp_path, timeout=STARTUP_BUDGET)
    session_id = SessionId.new()
    try:
        assert (await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))).live

        observations = await terminal.managed_observations()

        assert observations[0].session_id == session_id
        assert observations[0].project_id == ProjectId("opaque-editor")
        assert observations[0].profile_id == ProfileId("fake")
    finally:
        try:
            await gateway.mutate("kill-session", f"ra-{session_id}")
        except RuntimeError:
            pass


def _event_count(store: SQLiteSessionStore) -> int:
    return int(store._connection.execute("SELECT COUNT(*) FROM session_events").fetchone()[0])
