"""Safe reconciliation policy for durable records and trusted terminal observations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import (
    OrphanProvenance,
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
    records: tuple[SessionRecord, ...],
    observations: tuple[TerminalObservation, ...],
    *,
    also_known: frozenset[SessionId] = frozenset(),
) -> tuple[ReconciliationResult, ...]:
    """Derive safe states from terminal evidence without terminal mutation.

    `also_known` names sessions that exist in the store but are deliberately **not** being
    reconciled on this pass — today, the ones still inside their settle window. They must
    still count as known, because "known" here answers "does a row exist for this pane", not
    "is this pane being reconciled". Conflating the two made every launch look like an
    unknown pane for the length of the settle window: `_save_trusted_orphan` then tried to
    INSERT a row whose primary key already existed, the `UNIQUE` constraint raised, and the
    whole pass aborted — taking the runtime coordinator down with it, since it does not
    swallow that. Found by the Stage 4 gate's adversarial pass, reproduced against a real
    store.
    """
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
            # Unreachable from the real adapter, and kept deliberately. `codec.parse_pane`
            # derives both flags from one validated `pane_dead` field, so `live` and
            # `preserved` are exact complements and this `else` needs a pane that is neither.
            # Only a hand-built observation reaches it. Kept because it is the honest
            # fallthrough for a runtime that learns to report a third condition, and because
            # deleting it would make the next such runtime silently take the `live` branch.
            state, reason = SessionState.ORPHANED, "ambiguous_terminal"
        results.append(ReconciliationResult(record.session_id, state, reason))
    known = {record.session_id for record in records} | set(also_known)
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
        locks: SessionLocks | None = None,
    ) -> None:
        self._store = store
        self._settle_after = settle_after
        self._now = now
        self._confirm_ready = confirm_ready
        # The service's own locks, when the composition root shares them (`bootstrap.py`).
        # Optional so that every existing caller constructing this with a store alone keeps
        # working; absent, the settle window below is the only guard, which is what this
        # class had before.
        self._locks = locks

    async def reconcile(
        self, observations: tuple[TerminalObservation, ...]
    ) -> tuple[ReconciliationResult, ...]:
        stored = tuple(await self._store.list())
        records = tuple(record for record in stored if self._has_settled(record))
        settling = frozenset(record.session_id for record in stored) - {
            record.session_id for record in records
        }
        results = reconcile(records, observations, also_known=settling)
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
                await self._record_if_unchanged(record, event)
        return results

    async def _record_if_unchanged(self, record: SessionRecord, event: LifecycleEvent) -> None:
        """Write the repair under the session's own lock, and only if nothing moved.

        `_has_settled` is evaluated for every record at the top of the pass, but the loop
        above it awaits -- on `_is_ready`, and on each `record_event`. So a stop can begin
        *after* the settle check cleared a session and *before* this pass reaches its write,
        and the check would still say it was safe. Reproduced with two sessions: the
        reconciler yields inside its write to the first, an owner's graceful stop runs to
        completion on the second in that gap, and the pass then writes
        `reconciled_terminal_missing` to a record that is already ENDED.

        That is the same crash class this stage exists to close, so closing it halfway would
        have been worse than not claiming it. The settle check stays -- it is what keeps an
        in-flight record out of `records` entirely, which `also_known` depends on -- and the
        write is made atomic with respect to the service here.

        The state re-read is not redundant with the lock. A mutation that completed *before*
        this coroutine acquired the lock leaves nothing held to see, so the lock alone would
        admit a write against a record that has already moved on. When it has, this pass is
        working from a stale reading and the right answer is to do nothing: whatever moved
        the record knows more than this pass does, and the next pass looks again.

        Takes `for_session` alone and never `operation()`, so there is no lock-ordering cycle
        with `SessionService`, which takes them in that order.
        """
        if self._locks is None:
            await self._store.record_event(record.session_id, event)
            return
        async with self._locks.for_session(record.session_id):
            current = await self._store.get(record.session_id)
            if current is None or current.state is not record.state:
                return
            await self._store.record_event(record.session_id, event)

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

        **Two guards, because there are two kinds of unsettled record and a clock cannot
        tell them apart.**

        The first is the lock, and it is the one that was missing. A mutation that has a
        session id in hand holds `for_session` for its whole body -- `graceful_stop` records
        `GRACEFUL_STOP_REQUESTED`, awaits the terminal, then records a second event, all
        inside it. If that lock is held, a caller is between two writes *right now*, and no
        elapsed time makes reconciling it safe.

        The second is the settle window, and after the Stage 4 gate review its job is
        narrower than it first appears. `launch` and `resume` now take `for_session` for
        everything after the record exists, so they are covered by the lock like any other
        mutation; before that fix they were not, and a launch slower than the window was
        reconciled to FAILED underneath itself. What the window is left covering is the
        genuinely abandoned record -- one left in STARTING or STOP_REQUESTED by a process
        that has since died, which holds no lock because nothing is running, and which must
        be repaired rather than protected forever.

        **What was wrong before:** the window was the only guard, so it was doing both jobs
        and could only do one. `now - created_at` asks how old the *session* is, so the guard
        held for the first two minutes of a session's life and then switched itself off
        permanently. A launch was protected, because a launch happens at the beginning; a
        stop almost never was, because the owner stops a session after working in it. The
        deployed service produced exactly that:
        `InvalidTransition: pane_exited is not legal while session is running`, reaching the
        owner as "callback action failed while its pending notice was on screen".

        Every test in `tests/unit/application/test_reconcile.py` but one passed
        `settle_after=0`, which disables the window entirely. The exception
        (`test_a_session_inside_its_settle_window_is_not_mistaken_for_an_unknown_pane`) uses a
        real two-minute window on a *fresh* STARTING record -- the case the old form got
        right. So the axis it got wrong, an aged record past the window, was covered by
        nothing.

        The lock guard is only as wide as the process holding it. DEC-005 accepts a second
        writer -- the local terminal -- and no asyncio lock reaches it; that race is unchanged
        and is not what crashed here.
        """
        if record.state not in {SessionState.STARTING, SessionState.STOP_REQUESTED}:
            return True
        if self._locks is not None and self._locks.session_is_busy(record.session_id):
            return False
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
                # Keyword, not positional: provenance is the tenth field and every rebuild
                # that reached it positionally is exactly how it gets dropped.
                orphan_provenance=OrphanProvenance.ADOPTED,
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
            # Still a *timeout*, and it must stay one. DEC-022 split `GRACEFUL_STOP_NEVER_SENT`
            # out of this event for the case where nothing was ever signalled — but that is
            # `SessionService.graceful_stop` seeing `unknown_session` from the terminal, which
            # is a different producer from this one. Here the pane is live and the record has
            # been sitting in STOP_REQUESTED since a stop that was sent: the exit sequence went
            # out, the wait ran out, and the agent is still there. Two call sites recording two
            # events for what reads like one failure is exactly the shape that invites a
            # "consistency" fix, so the difference is written here rather than left to be
            # inferred.
            #
            # **Not exhaustive, and the gap is worth knowing rather than papering over.**
            # `graceful_stop` writes GRACEFUL_STOP_REQUESTED — which persists STOP_REQUESTED —
            # *before* it calls the terminal, so a process that dies in that window leaves a
            # durable STOP_REQUESTED behind a stop that never left the host. The next pass sees
            # a live pane and lands here, recording a timeout for it. That is the same
            # over-claim DEC-022 removed from the other producer, surviving in a narrower
            # crash-recovery case this branch cannot tell apart: the record stores the event,
            # not the observation, so nothing here can distinguish it. Named because it is
            # exactly the reasoning a reader needs, and it is *not* an argument for merging the
            # two events — that would give the ordinary case the wrong name to fix the rare one.
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

    def session_is_busy(self, session_id: SessionId) -> bool:
        """Whether a mutation is in flight on this session right now.

        Deliberately does **not** use `for_session`, which is a `setdefault` and would mint a
        lock for every session merely asked about -- turning a read into an allocation on a
        map that lives as long as the process.

        This is what lets `ReconciliationService` stay off a record whose own caller is
        between two writes. Reading `locked()` is sound here because both sides run on the
        one event loop in the one process: the reconciler is a task beside the service, not a
        thread, so there is no window between this answer and acting on it. It says nothing
        about the *other* writer DEC-005 accepts -- the local TUI drives its own
        `SessionService` in a separate process with its own `SessionLocks`, and no asyncio
        lock spans processes. ("The local TUI", not "the local terminal": this file uses
        *terminal* for the `TerminalPort` both services share in-process, which is a
        different thing.)
        """
        lock = self._locks.get(session_id)
        return lock is not None and lock.locked()

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
