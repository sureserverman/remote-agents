"""Reconciliation tests: terminal evidence wins and ambiguity is read-only."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from remote_agents.application.reconcile import (
    ReconciliationResult,
    ReconciliationService,
    SessionLocks,
    reconcile,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.state_machine import LifecycleEvent, transition
from remote_agents.ports.terminal import TerminalObservation


class InMemoryStore:
    def __init__(self, records: tuple[SessionRecord, ...]) -> None:
        self.records = {record.session_id: record for record in records}
        self.events: list[LifecycleEvent] = []

    async def next_sequence(self, project_id: ProjectId, profile_id: ProfileId) -> int:
        return 1 + sum(
            item.project_id == project_id and item.profile_id == profile_id
            for item in self.records.values()
        )

    async def save(self, item: SessionRecord) -> None:
        self.records[item.session_id] = item

    async def list(self) -> tuple[SessionRecord, ...]:
        return tuple(self.records.values())

    async def record_event(self, session_id: SessionId, event: LifecycleEvent) -> SessionRecord:
        current = self.records[session_id]
        updated = SessionRecord(
            current.session_id,
            current.project_id,
            current.profile_id,
            current.display,
            transition(current.state, event).to_state,
            current.created_at,
        )
        self.records[session_id] = updated
        self.events.append(event)
        return updated


def record(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


def test_reconcile_treats_terminal_as_liveness_authority() -> None:
    live, missing, preserved, ambiguous = record(), record(), record(), record()
    observations = (
        TerminalObservation(live.session_id, live=True, preserved=False),
        TerminalObservation(preserved.session_id, live=False, preserved=True),
        TerminalObservation(ambiguous.session_id, live=False, preserved=False),
    )

    results = {
        result.session_id: result
        for result in reconcile((live, missing, preserved, ambiguous), observations)
    }

    assert results[live.session_id].state is SessionState.RUNNING
    assert results[missing.session_id].state is SessionState.ENDED
    assert results[preserved.session_id].state is SessionState.PRESERVED
    assert results[ambiguous.session_id].state is SessionState.ORPHANED


def test_reconcile_quarantines_unknown_terminal_session() -> None:
    unknown = SessionId.new()

    result = reconcile((), (TerminalObservation(unknown, live=True, preserved=False),))

    assert result == (ReconciliationResult(unknown, SessionState.ORPHANED, "unknown_session"),)


async def test_reconciliation_persists_each_deterministic_change_once() -> None:
    starting, running, unknown = record(SessionState.STARTING), record(), SessionId.new()
    store = InMemoryStore((starting, running))
    service = ReconciliationService(store, settle_after=timedelta(0))
    observations = (
        TerminalObservation(starting.session_id, live=True, preserved=False),
        TerminalObservation(
            unknown,
            live=True,
            preserved=False,
            project_id=ProjectId("opaque-editor"),
            profile_id=ProfileId("claude"),
        ),
    )

    first = await service.reconcile(observations)
    second = await service.reconcile(observations)

    assert {result.session_id: result.state for result in first} == {
        starting.session_id: SessionState.RUNNING,
        running.session_id: SessionState.ENDED,
        unknown: SessionState.ORPHANED,
    }
    assert {result.session_id: result.state for result in second} == {
        starting.session_id: SessionState.RUNNING,
        running.session_id: SessionState.ENDED,
        unknown: SessionState.ORPHANED,
    }
    assert store.records[starting.session_id].state is SessionState.RUNNING
    assert store.records[running.session_id].state is SessionState.ENDED
    assert store.records[unknown].state is SessionState.ORPHANED
    assert store.events == [LifecycleEvent.READY, LifecycleEvent.RECONCILED_TERMINAL_MISSING]


async def test_a_live_pane_that_is_not_ready_is_not_promoted_to_running() -> None:
    """The bug this exists for: a session blocked on a question is live, and not running.

    Reconciliation's `terminal_live` promotion reads a live pane as a working agent, which
    is right for the case its comment names -- an agent that is slow or quiet, recorded
    FAILED while its pane keeps working. It is wrong for an agent stopped dead on a prompt
    it cannot answer: the pane is live, the process is up, and nothing is running.

    Observed in the wild on 2026-08-14: a `claude-remote` session launched into a directory
    Claude Code had not been trusted for failed its readiness check correctly, then a
    service restart ran reconciliation, which promoted it FAILED -> RUNNING. The bot then
    reported a session as running while it sat on an unanswered dialog. `confirm_ready`
    already distinguishes the two; reconciliation simply never asked it.
    """
    failed = record(SessionState.FAILED)
    store = InMemoryStore((failed,))
    asked: list[SessionId] = []

    async def never_ready(session_id, profile_id):
        del profile_id
        asked.append(session_id)
        return TerminalObservation(session_id, live=False, preserved=False, detail="not_ready")

    service = ReconciliationService(store, settle_after=timedelta(0), confirm_ready=never_ready)

    await service.reconcile((TerminalObservation(failed.session_id, live=True, preserved=False),))

    assert asked == [failed.session_id], "readiness must actually be consulted"
    assert store.records[failed.session_id].state is SessionState.FAILED
    assert store.events == [], "no promotion, so no lifecycle event"


async def test_a_live_pane_under_a_stop_request_is_a_timeout_and_not_a_stop_never_sent() -> None:
    """The one branch DEC-022 deliberately left recording the old event.

    `SessionService.graceful_stop` stopped writing `GRACEFUL_STOP_TIMED_OUT` for
    `unknown_session`, because nothing was signalled there. This producer is the other one and
    it keeps the event: it finds a live pane under a record that has been in STOP_REQUESTED
    since a stop that was sent, which is a real timeout.

    Written because the argument for keeping them apart lived only in a comment. Two call
    sites recording two events for what reads like the same failure is the shape that invites
    a "consistency" edit, and until this existed such an edit passed the whole suite — the
    defence was prose a refactor never has to read. Found by the Stage 2 gate evaluator, which
    noted the domain test one layer down had already been given exactly this treatment.
    """
    stopping = record(SessionState.STOP_REQUESTED)
    store = InMemoryStore((stopping,))
    service = ReconciliationService(store, settle_after=timedelta(0))

    await service.reconcile((TerminalObservation(stopping.session_id, live=True, preserved=False),))

    assert store.events == [LifecycleEvent.GRACEFUL_STOP_TIMED_OUT], (
        "reconciliation saw a live pane under a stop that was sent — that is a timeout, and "
        "recording GRACEFUL_STOP_NEVER_SENT here would claim nothing left the host"
    )
    assert store.records[stopping.session_id].state is SessionState.RUNNING


async def test_a_live_pane_that_is_ready_is_still_promoted() -> None:
    """The repair the promotion exists for must survive the new check."""
    failed = record(SessionState.FAILED)
    store = InMemoryStore((failed,))

    async def ready(session_id, profile_id):
        del profile_id
        return TerminalObservation(session_id, live=True, preserved=False)

    service = ReconciliationService(store, settle_after=timedelta(0), confirm_ready=ready)

    await service.reconcile((TerminalObservation(failed.session_id, live=True, preserved=False),))

    assert store.records[failed.session_id].state is SessionState.RUNNING
    assert store.events == [LifecycleEvent.READY]


async def test_reconciliation_never_creates_an_orphan_without_trusted_identity() -> None:
    store = InMemoryStore(())
    service = ReconciliationService(store, settle_after=timedelta(0))

    results = await service.reconcile(
        (TerminalObservation(SessionId.new(), live=True, preserved=False),)
    )

    assert results[0].state is SessionState.ORPHANED
    assert store.records == {}


async def test_per_session_lock_serializes_concurrent_mutations() -> None:
    session_id = SessionId.new()
    locks = SessionLocks()
    order: list[str] = []

    async def mutate(name: str) -> None:
        async with locks.for_session(session_id):
            order.append(f"{name}-start")
            await asyncio.sleep(0)
            order.append(f"{name}-end")

    await asyncio.gather(mutate("first"), mutate("second"))

    assert order in (
        ["first-start", "first-end", "second-start", "second-end"],
        ["second-start", "second-end", "first-start", "first-end"],
    )


async def test_mutation_drain_waits_for_active_operation_and_closes_admission() -> None:
    locks = SessionLocks()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def active_operation() -> None:
        async with locks.operation():
            entered.set()
            await release.wait()

    operation = asyncio.create_task(active_operation())
    await entered.wait()
    draining = asyncio.create_task(locks.drain())
    await asyncio.sleep(0)
    assert not draining.done()

    release.set()
    await operation
    await draining

    with pytest.raises(RuntimeError, match="mutations are draining"):
        async with locks.operation():
            pass
