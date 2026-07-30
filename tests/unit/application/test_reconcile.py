"""Reconciliation tests: terminal evidence wins and ambiguity is read-only."""

import asyncio
from datetime import UTC, datetime

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
    service = ReconciliationService(store)
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


async def test_reconciliation_never_creates_an_orphan_without_trusted_identity() -> None:
    store = InMemoryStore(())
    service = ReconciliationService(store)

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
