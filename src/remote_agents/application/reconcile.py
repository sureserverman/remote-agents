"""Safe reconciliation policy for durable records and trusted terminal observations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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


_ConfirmReady = Callable[[SessionId, ProfileId], Awaitable[TerminalObservation]]


class ReconciliationService:
    """Persist deterministic terminal evidence and only quarantine trusted unknown tags."""

    def __init__(
        self,
        store: SessionStore,
        *,
        settle_after: timedelta = timedelta(minutes=2),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        confirm_ready: _ConfirmReady | None = None,
    ) -> None:
        self._store = store
        self._settle_after = settle_after
        self._now = now
        self._confirm_ready = confirm_ready

    async def reconcile(
        self, observations: tuple[TerminalObservation, ...]
    ) -> tuple[ReconciliationResult, ...]:
        records = tuple(record for record in await self._store.list() if self._has_settled(record))
        results = reconcile(records, observations)
        by_id = {record.session_id: record for record in records}
        for result in results:
            record = by_id.get(result.session_id)
            if record is None:
                await self._save_trusted_orphan(result, observations)
                continue
            event = _event_for_reconciliation(record, result)
            if event is LifecycleEvent.READY and not await self._is_ready(record):
                # A live pane is not a running agent. The promotion below reads pane
                # liveness as proof the agent recovered, which is true for the case it was
                # written for -- slow or quiet output judged FAILED inside a bounded window
                # -- and false for an agent stopped on a question it cannot answer. Both
                # look identical from `managed_observations`, so the difference has to come
                # from the readiness check that already knows it.
                continue
            if event is not None:
                await self._store.record_event(record.session_id, event)
        return results

    async def _is_ready(self, record: SessionRecord) -> bool:
        """Whether the agent behind a live pane is actually ready to be called RUNNING.

        Answers True when no check was supplied, which keeps every existing caller and the
        pure `reconcile()` function exactly as they were: this narrows a promotion, and a
        composition that does not wire the check gets the old behaviour rather than a
        silently different one.
        """
        if self._confirm_ready is None:
            return True
        try:
            return (await self._confirm_ready(record.session_id, record.profile_id)).live
        except Exception:
            # A readiness check that cannot run is not evidence of readiness. Refusing the
            # promotion leaves the record where it was, which is the recoverable direction:
            # the next pass tries again, and nothing has claimed a blocked agent is running.
            return False

    def _has_settled(self, record: SessionRecord) -> bool:
        """Leave a launch or stop that is still running to the call that started it.

        A pane exists as soon as tmux creates it, well before its agent reports ready, and
        the same holds while a graceful stop waits for the pane to exit. Reconciling either
        window would overwrite a state its own caller is about to record, and that caller's
        event is then an illegal transition from the state written underneath it.
        """
        if record.state not in {SessionState.STARTING, SessionState.STOP_REQUESTED}:
            return True
        return self._now() - record.created_at >= self._settle_after

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
        # FAILED belongs here as much as STARTING does. Readiness is judged from a pane's
        # output within a bounded window, so an agent that is slow or quiet is recorded as
        # having failed while its pane keeps working; the matrix has always allowed
        # FAILED -> RUNNING for exactly this repair, and nothing else ever issued it. A
        # live pane means a live process, because an agent that exits kills its pane.
        if record.state in {SessionState.STARTING, SessionState.FAILED}:
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
    """The asyncio locks `SessionService` takes around the mutations it issues.

    Every mutation it issues, not only the ones that end a session: the previous wording said
    "destructive mutations", which read as though a graceful stop were outside the lock, and it
    is not. But the set is bounded by the **caller** rather than by the kind of change, and
    saying "every state-changing mutation" would assert a guarantee this class does not give.
    `ReconciliationService` above is the counter-example, and it is not a hypothetical one: it
    is constructed with a store and a readiness check and no locks at all (`bootstrap.py`), runs
    on a timer beside the service (`_reconcile_periodically`), and writes `record_event`
    directly, so a reconciliation pass racing an owner's stop on the same session is not
    serialized by anything here.

    Which lock covers what also differs, because a session id is not always in hand yet:
    `launch` and `resume` take `operation()` alone — the record does not exist to key on — and
    `resume` adds `for_conversation`. `for_session` covers the mutations of a record that is
    already stored.
    """

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
