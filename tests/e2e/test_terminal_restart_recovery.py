"""Restart reconciliation against an isolated tmux server and real SQLite projection."""

from datetime import UTC, datetime, timedelta

import pytest
from test_terminal_launch import STARTUP_BUDGET, make_terminal

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.application.commands import ForceStopCommand
from remote_agents.application.errors import StopNotPermittedError
from remote_agents.application.reconcile import ReconciliationService
from remote_agents.application.services import SessionService
from remote_agents.application.session_actions import FORCE, available_actions
from remote_agents.domain.models import (
    OrphanProvenance,
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.state_machine import LifecycleEvent


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
            await gateway.destroy(session_id)
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
        assert persisted.orphan_provenance is OrphanProvenance.ADOPTED, (
            "the branch DEC-020 turns on has to be stamped by the real adapter, not only by "
            "the unit fakes"
        )
    finally:
        try:
            await gateway.destroy(session_id)
        except RuntimeError:
            pass


async def test_an_adopted_session_can_actually_be_force_stopped_through_the_real_terminal(
    tmp_path,
):
    """The seam DEC-020 exists to close, end to end against real tmux and real SQLite.

    Every other test of this capability stops at one side of a boundary: the unit tests prove
    the policy offers force for an adopted record, and the adoption test above proves
    reconciliation stamps one. Neither proves the **session id `_save_trusted_orphan` writes
    is an id the terminal can actually resolve for a kill** — and that is precisely the class
    this project's own seam-defect note describes, because
    it lives between two correct halves.

    Found missing by the Stage 4 gate's evaluator, which verified the behaviour by hand and
    recorded the absence of a test as the finding.
    """
    terminal, gateway = make_terminal(tmp_path, timeout=STARTUP_BUDGET)
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    service = SessionService(store, terminal)
    reconciler = ReconciliationService(store, settle_after=timedelta(0))
    session_id = SessionId.new()
    try:
        assert (await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))).live
        await reconciler.reconcile(await terminal.managed_observations())
        adopted = await store.get(session_id)
        assert adopted is not None
        assert adopted.orphan_provenance is OrphanProvenance.ADOPTED
        assert FORCE in available_actions(adopted.state, adopted.orphan_provenance)

        await service.force_stop(ForceStopCommand(session_id))

        ended = await store.get(session_id)
        assert ended is not None
        assert ended.state is SessionState.ENDED
        assert ended.terminal_reason == "verified_force_stop"
        assert ended.orphan_provenance is OrphanProvenance.ADOPTED, (
            "the audit trail must still say what kind of ORPHANED was killed"
        )
        assert not [
            item for item in await terminal.managed_observations() if item.session_id == session_id
        ], "the pane must actually be gone, not merely marked gone"
    finally:
        try:
            await gateway.destroy(session_id)
        except RuntimeError:
            pass


async def test_a_muddled_evidence_orphan_is_refused_by_the_service_not_only_by_the_surfaces(
    tmp_path,
):
    """The other half of the seam: the conservative branch, refused at the layer that kills.

    Both surfaces gate on `available_actions` before calling, so this refusal is only ever
    reached by a caller that bypassed them. That is exactly what makes it worth pinning — it
    is the guard nothing else exercises.
    """
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    service = SessionService(store, FakeTerminal())
    session_id = SessionId.new()
    await store.save(starting_record(session_id))
    await store.record_event(session_id, LifecycleEvent.AMBIGUOUS_TERMINAL_EVIDENCE)

    held = await store.get(session_id)
    assert held is not None
    assert held.orphan_provenance is OrphanProvenance.AMBIGUOUS

    with pytest.raises(StopNotPermittedError):
        await service.force_stop(ForceStopCommand(session_id))

    assert (await store.get(session_id)).state is SessionState.ORPHANED


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
            await gateway.destroy(session_id)
        except RuntimeError:
            pass


def _event_count(store: SQLiteSessionStore) -> int:
    return int(store._connection.execute("SELECT COUNT(*) FROM session_events").fetchone()[0])
