"""Safe reconciliation policy for durable records and trusted terminal observations."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import (
    ProfileId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.state_machine import LifecycleEvent
from remote_agents.ports.session_store import SessionStore
from remote_agents.ports.terminal import TerminalObservation


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    session_id: SessionId
    state: SessionState
    reason: str


def reconcile(
    records: tuple[SessionRecord, ...], observations: tuple[TerminalObservation, ...]
) -> tuple[ReconciliationResult, ...]:
    """Derive safe states from terminal evidence without terminal mutation."""
    by_id = {observation.session_id: observation for observation in observations}
    results: list[ReconciliationResult] = []
    for record in records:
        if record.state is SessionState.ORPHANED:
            results.append(ReconciliationResult(record.session_id, record.state, "quarantined"))
            continue
        observation = by_id.get(record.session_id)
        if observation is None:
            state, reason = (
                (SessionState.FAILED, "startup_missing")
                if record.state is SessionState.STARTING
                else (SessionState.ENDED, "terminal_missing")
            )
        elif observation.preserved:
            state, reason = SessionState.PRESERVED, "pane_dead"
        elif observation.live:
            state, reason = SessionState.RUNNING, "terminal_live"
        else:
            state, reason = SessionState.ORPHANED, "ambiguous_terminal"
        results.append(ReconciliationResult(record.session_id, state, reason))
    known = {record.session_id for record in records}
    results.extend(
        ReconciliationResult(observation.session_id, SessionState.ORPHANED, "unknown_session")
        for observation in observations
        if observation.session_id not in known
    )
    return tuple(results)


class ReconciliationService:
    """Persist deterministic terminal evidence and only quarantine trusted unknown tags."""

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def reconcile(
        self, observations: tuple[TerminalObservation, ...]
    ) -> tuple[ReconciliationResult, ...]:
        records = tuple(await self._store.list())
        results = reconcile(records, observations)
        by_id = {record.session_id: record for record in records}
        for result in results:
            record = by_id.get(result.session_id)
            if record is None:
                await self._save_trusted_orphan(result, observations)
                continue
            event = _event_for_reconciliation(record, result)
            if event is not None:
                await self._store.record_event(record.session_id, event)
        return results

    async def _save_trusted_orphan(
        self,
        result: ReconciliationResult,
        observations: tuple[TerminalObservation, ...],
    ) -> None:
        if result.reason != "unknown_session":
            return
        observation = next(
            (item for item in observations if item.session_id == result.session_id), None
        )
        if observation is None or observation.project_id is None or observation.profile_id is None:
            return
        sequence = await self._store.next_sequence(observation.project_id, observation.profile_id)
        await self._store.save(
            SessionRecord(
                result.session_id,
                observation.project_id,
                observation.profile_id,
                SessionDisplayIdentity(
                    str(observation.project_id), str(observation.profile_id), "recovered", sequence
                ),
                SessionState.ORPHANED,
                datetime.now(UTC),
            )
        )


def _event_for_reconciliation(
    record: SessionRecord, result: ReconciliationResult
) -> LifecycleEvent | None:
    """Translate only safe, documented divergence repairs into lifecycle events."""
    if result.state is record.state:
        return None
    if result.reason == "startup_missing" and record.state is SessionState.STARTING:
        return LifecycleEvent.STARTUP_ERROR
    if result.reason == "terminal_live":
        if record.state is SessionState.STARTING:
            return LifecycleEvent.READY
        if record.state is SessionState.STOP_REQUESTED:
            return LifecycleEvent.GRACEFUL_STOP_TIMED_OUT
    if result.reason == "pane_dead" and record.state is SessionState.RUNNING:
        return LifecycleEvent.RECONCILED_PANE_DEAD
    if result.reason == "terminal_missing" and record.state in {
        SessionState.RUNNING,
        SessionState.STOP_REQUESTED,
        SessionState.PRESERVED,
    }:
        return LifecycleEvent.RECONCILED_TERMINAL_MISSING
    if result.reason == "ambiguous_terminal" and record.state not in {
        SessionState.ENDED,
        SessionState.ORPHANED,
    }:
        return LifecycleEvent.AMBIGUOUS_TERMINAL_EVIDENCE
    return None


class SessionLocks:
    """Per-session asyncio locks serializing concurrent destructive mutations."""

    def __init__(self) -> None:
        self._locks: dict[SessionId, asyncio.Lock] = {}
        self._conversation_locks: dict[tuple[ProfileId, ProviderConversationId], asyncio.Lock] = {}
        self._active_operations = 0
        self._accepting_operations = True
        self._drained = asyncio.Event()
        self._drained.set()

    def for_session(self, session_id: SessionId) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def for_conversation(
        self, profile_id: ProfileId, source_id: ProviderConversationId
    ) -> asyncio.Lock:
        return self._conversation_locks.setdefault((profile_id, source_id), asyncio.Lock())

    @asynccontextmanager
    async def operation(self):
        """Track a mutation so shutdown can finish it without admitting new work."""
        if not self._accepting_operations:
            raise RuntimeError("mutations are draining")
        self._active_operations += 1
        self._drained.clear()
        try:
            yield
        finally:
            self._active_operations -= 1
            if self._active_operations == 0:
                self._drained.set()

    async def drain(self) -> None:
        """Close the mutation admission gate and wait for active operations to finish."""
        self._accepting_operations = False
        await self._drained.wait()
