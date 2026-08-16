"""Reconciliation tests: terminal evidence wins and ambiguity is read-only."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from remote_agents.application.reconcile import (
    ReconciliationResult,
    ReconciliationService,
    SessionLocks,
    reconcile,
)
from remote_agents.domain.models import (
    OrphanProvenance,
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

    async def get(self, session_id: SessionId) -> SessionRecord | None:
        # On the port since before this fake existed, and unimplemented here until the
        # reconciler needed it -- the same class of gap the `record_event` comment below
        # names. A fake narrower than the port it stands in for passes tests the production
        # path would fail.
        return self.records.get(session_id)

    async def list(self) -> tuple[SessionRecord, ...]:
        return tuple(self.records.values())

    async def record_event(self, session_id: SessionId, event: LifecycleEvent) -> SessionRecord:
        # `replace` rather than a positional rebuild: this fake used to reconstruct the record
        # from its first six fields and silently drop every later one, which is the same
        # defect Task 4.1 closed in the real store. A fake that loses a field the store keeps
        # passes tests the production path would fail.
        current = self.records[session_id]
        updated = replace(current, state=transition(current.state, event).to_state)
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
    it keeps the event: it finds a live pane under a record in STOP_REQUESTED, which in the
    ordinary case is a stop that was sent and did not take.

    **It cannot prove that, and this test does not claim it does.** The record stores the
    event, not the observation that produced it, so a record left in STOP_REQUESTED by a crash
    between `graceful_stop`'s first write and its terminal call is indistinguishable here —
    the same argument DEC-022 makes for why historical rows are not migrated, in a narrower
    place. What is pinned is only that *this* producer keeps recording the timeout, because
    the alternative would name every ordinary case wrongly in order to catch the rare one.

    Written because the argument for keeping the two producers apart lived only in a comment.
    Two call sites recording two events for what reads like the same failure is the shape that
    invites a "consistency" edit, and until this existed such an edit passed the whole suite —
    the defence was prose a refactor never has to read. Found by the Stage 2 gate evaluator,
    which noted the domain test one layer down already had exactly this treatment.
    """
    stopping = record(SessionState.STOP_REQUESTED)
    store = InMemoryStore((stopping,))
    service = ReconciliationService(store, settle_after=timedelta(0))

    await service.reconcile((TerminalObservation(stopping.session_id, live=True, preserved=False),))

    assert store.events == [LifecycleEvent.GRACEFUL_STOP_TIMED_OUT], (
        "this producer must keep recording a timeout: it cannot see whether the exit sequence "
        "was sent, and GRACEFUL_STOP_NEVER_SENT would assert nothing left the host for every "
        "ordinary case in order to be right about the rare one"
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


async def test_a_session_inside_its_settle_window_is_not_mistaken_for_an_unknown_pane() -> None:
    """A launching session is *known*, even while it is deliberately not being reconciled.

    The settle filter exists so a pass does not overwrite a state its own caller is about to
    record. But "known" answers a different question — does a row exist for this pane — and
    computing it from the filtered set conflated the two. Every launch then looked like an
    unknown pane for the whole settle window, `_save_trusted_orphan` tried to INSERT a
    primary key that already existed, and the UNIQUE constraint aborted the entire pass.
    `RuntimeCoordinator._reconcile_once` does not swallow that, so it took the runtime down.

    Reproduced against a real SQLite store by the Stage 4 gate's adversarial pass; pinned
    here at the unit tier because this is where the classification is decided.
    """
    settling = record(SessionState.STARTING)
    store = InMemoryStore((settling,))
    service = ReconciliationService(store, settle_after=timedelta(minutes=2))

    results = await service.reconcile(
        (
            TerminalObservation(
                settling.session_id,
                live=True,
                preserved=False,
                project_id=ProjectId("opaque-editor"),
                profile_id=ProfileId("claude"),
            ),
        )
    )

    assert [item.reason for item in results] == [], (
        "a settling session should be skipped entirely, not classified as an unknown pane"
    )
    assert store.records[settling.session_id].state is SessionState.STARTING
    assert store.events == []


async def test_an_adopted_orphan_records_which_producer_created_it() -> None:
    """The whole of DEC-020 rests on this stamp: it is what separates a live adopted agent
    from a muddled-evidence record, and it can only be known here, at the moment of adoption.
    """
    store = InMemoryStore(())
    service = ReconciliationService(store, settle_after=timedelta(0))
    unknown = SessionId.new()

    await service.reconcile(
        (
            TerminalObservation(
                unknown,
                live=True,
                preserved=False,
                project_id=ProjectId("opaque-editor"),
                profile_id=ProfileId("claude"),
            ),
        )
    )

    assert store.records[unknown].state is SessionState.ORPHANED
    assert store.records[unknown].orphan_provenance is OrphanProvenance.ADOPTED


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


async def test_a_stop_in_flight_is_left_alone_however_old_the_session_is() -> None:
    """The guard must key on a caller being mid-flight, not on the session's age.

    `_has_settled` exists to keep a reconciliation pass off a record whose own caller is
    between two writes -- its docstring says so outright: "Reconciling either window would
    overwrite a state its own caller is about to record, and that caller's event is then an
    illegal transition from the state written underneath it." That is exactly the crash the
    deployed service was producing, `InvalidTransition: pane_exited is not legal while
    session is running`, reaching the owner as "callback action failed while its pending
    notice was on screen".

    Before the fix the only guard was `now - record.created_at >= settle_after` -- the age of
    the *session*, not the progress of the *caller*. So it held for the first two minutes of a
    session's life and then switched itself off permanently: a launch was protected because a
    launch happens at the beginning, and a stop almost never was, because the owner stops a
    session after working in it. Every other test in this file passes `settle_after=0`, which
    disables the window entirely, which is why none of them caught it.
    """
    stopping = replace(
        record(SessionState.STOP_REQUESTED),
        created_at=datetime.now(UTC) - timedelta(hours=6),
    )
    store = InMemoryStore((stopping,))
    locks = SessionLocks()
    service = ReconciliationService(store, settle_after=timedelta(0), locks=locks)
    observation = (TerminalObservation(stopping.session_id, live=False, preserved=False),)

    # Exactly what `SessionService.graceful_stop` holds across its two `record_event` calls.
    async with locks.for_session(stopping.session_id):
        await service.reconcile(observation)

    assert store.events == [], "the reconciler wrote underneath an in-flight stop"
    assert store.records[stopping.session_id].state is SessionState.STOP_REQUESTED


async def test_a_record_no_caller_is_holding_is_still_reconciled() -> None:
    """The other direction, which is what stops the fix being 'never reconcile anything'.

    A guard that never lets the reconciler write is trivially crash-free and silently
    abandons every record that really is stuck -- a launch that died leaves STARTING behind,
    and no owner action can resolve it. Once the lock is released there is no caller left to
    protect, so the pass must act.
    """
    stopping = replace(
        record(SessionState.STOP_REQUESTED),
        created_at=datetime.now(UTC) - timedelta(hours=6),
    )
    store = InMemoryStore((stopping,))
    locks = SessionLocks()
    service = ReconciliationService(store, settle_after=timedelta(0), locks=locks)
    observation = (TerminalObservation(stopping.session_id, live=False, preserved=False),)

    async with locks.for_session(stopping.session_id):
        await service.reconcile(observation)
    assert store.events == []

    await service.reconcile(observation)

    # Not-live and not-preserved is *ambiguous* evidence rather than a confirmed ending, so
    # the record is held aside rather than closed. The point here is only that it acts.
    assert store.events == [LifecycleEvent.AMBIGUOUS_TERMINAL_EVIDENCE]
    assert store.records[stopping.session_id].state is SessionState.ORPHANED


async def test_asking_whether_a_session_is_busy_does_not_mint_a_lock_for_it() -> None:
    """`for_session` is a setdefault, so a read through it would allocate on every pass.

    The reconciler asks about every stored record on every pass, for the life of a process
    designed to run for weeks. Routing that through `for_session` would grow the lock map by
    one entry per session asked about and never release it.
    """
    locks = SessionLocks()
    session_id = SessionId.new()

    assert locks.session_is_busy(session_id) is False
    assert locks._locks == {}
