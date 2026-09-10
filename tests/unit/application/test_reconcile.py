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


# --- Pane provenance (Stage 3) ---------------------------------------------------------
#
# Reconciliation writes lifecycle state on a timer, so what it can and cannot see decides
# whether a live session survives a pass. Under the swap console a pane can be *hosted* by
# a session that is not its own, and the failure to avoid is reading that as absence: a
# displaced agent is running, not gone, and ending its record would be this service
# retiring a session nobody stopped.


def observed(session_id: SessionId, *, host: str | None = None, live: bool = True):
    return TerminalObservation(
        session_id,
        live,
        not live,
        project_id=ProjectId("opaque-editor"),
        profile_id=ProfileId("claude"),
        host_session=host if host is not None else f"ra-{session_id}",
    )


def running_record(session_id: SessionId) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "displaced", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


def test_a_displaced_pane_reconciles_to_running_not_to_gone() -> None:
    """Identity decides, location does not. The pane is in the console; the agent is fine."""
    session_id = SessionId.new()

    results = reconcile((running_record(session_id),), (observed(session_id, host="ra-console"),))

    assert [(item.state, item.reason) for item in results] == [
        (SessionState.RUNNING, "terminal_live")
    ]


def test_a_displaced_pane_and_a_home_pane_reconcile_identically() -> None:
    """The host is provenance, never a lifecycle input — if it changed the verdict, moving a
    pane would move a session's state, which is the coupling this whole sub-plan removes."""
    session_id = SessionId.new()
    record = running_record(session_id)

    at_home = reconcile((record,), (observed(session_id),))
    displaced = reconcile((record,), (observed(session_id, host="ra-console"),))

    assert [(r.state, r.reason) for r in at_home] == [(r.state, r.reason) for r in displaced]


def test_an_absent_pane_still_ends_the_record() -> None:
    """The other half, and the one that must not be weakened while making room for the
    first: no observation at all is still evidence that the session is over."""
    session_id = SessionId.new()

    results = reconcile((running_record(session_id),), ())

    assert [(item.state, item.reason) for item in results] == [
        (SessionState.ENDED, "terminal_missing")
    ]


def test_an_unmarked_console_pane_produces_no_observation_at_all() -> None:
    """Asserted where it is decided rather than here: `inventory` drops a console line that
    carries no managed mark, so it never becomes a `TerminalObservation` and cannot be
    reconciled into anything. This test pins the consequence — an empty observation set for
    a console-only server leaves an empty result — so a future widening of that drop shows
    up as a reconciliation change rather than only as an inventory one."""
    assert reconcile((), ()) == ()


def test_a_displaced_pane_with_no_record_is_adopted_the_same_as_a_home_one() -> None:
    """DEC-020's adopted orphan, and location plays no part in it.

    The pane is trustworthy because it carries its own mark, which is true wherever it is
    hosted — so being found in the console neither helps nor hinders adoption.

    **The host is not persisted, and an earlier name for this test said it was.** Nothing on
    `SessionRecord` or `OrphanProvenance` can hold it, and `_save_trusted_orphan` writes
    neither; `host_session` lives only for the length of a pass. Naming a test for a
    guarantee the code does not make is worse than having no test, because the next reader
    counts it as coverage — so this one is named for what it checks, and the absence is
    stated rather than left to be discovered.
    """
    session_id = SessionId.new()

    results = reconcile((), (observed(session_id, host="ra-console"),))

    assert [(item.state, item.reason) for item in results] == [
        (SessionState.ORPHANED, "unknown_session")
    ]


def test_reconciliation_never_reads_the_host_at_all() -> None:
    """Structural, and a supplement rather than the guarantee itself.

    `host_session` is provenance. The moment a lifecycle decision consults it, moving a pane
    moves a session's state — a displaced agent could reconcile to something other than what
    a home one does, which is precisely the coupling pane addressing was introduced to
    remove.

    What this catches is the literal identifier appearing in the module, which is how the
    coupling would most likely arrive. What it cannot catch is the same coupling under
    another name — a `getattr`, or host-derived data carried through `detail` — and it would
    fire falsely on a comment that merely mentions the field. The real guarantee is the
    verdict comparison above; this is the cheap tripwire in front of it, and saying so is the
    difference between a supplement and a false floor.
    """
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[3]
        / "src"
        / "remote_agents"
        / "application"
        / "reconcile.py"
    ).read_text(encoding="utf-8")

    assert "host_session" not in source, (
        "reconcile.py now reads host_session. It is provenance for a reader, not evidence "
        "for a decision: a session's state must not depend on which window is showing it."
    )


async def test_a_running_record_whose_pane_shows_a_dialog_is_corrected_to_untrusted() -> None:
    """The pane is live and the record already says RUNNING, so nothing else would look.

    `_event_for_reconciliation` returns None the moment the observed state matches the
    record's, which is the right answer for every reason *except* this one: a live pane is
    not one fact but two, and which of them it is only the readiness check knows. Without
    this the late-dialog race (claude-remote prints its banner before its question) leaves a
    green row over an agent that will never run.
    """
    running = record(SessionState.RUNNING)
    store = InMemoryStore((running,))

    async def blocked(session_id, profile_id):
        del profile_id
        return TerminalObservation(session_id, live=True, preserved=False, awaiting_trust=True)

    service = ReconciliationService(store, settle_after=timedelta(0), confirm_ready=blocked)

    await service.reconcile((TerminalObservation(running.session_id, live=True, preserved=False),))

    assert store.records[running.session_id].state is SessionState.UNTRUSTED
    assert store.events == [LifecycleEvent.TRUST_REQUIRED]


async def test_a_long_running_session_is_not_re_labelled_from_what_its_screen_shows() -> None:
    """**This one happened.** Session `8f4e954d`, 2026-09-09: `ready` 19:34, `untrusted` 22:51.

    Nothing happened to that agent at 22:51. It was a healthy `claude-remote` that had been
    working for three hours and seventeen minutes, and what was on its screen at that moment
    was this project's own trust-dialog strings being written into files. A reconciliation pass
    read the pane, matched the dialog, and rewrote the lifecycle record of a working session.

    The correction above exists for a **race**: `claude-remote` prints its readiness banner
    before its question renders, so a launch can be recorded RUNNING over an agent that is in
    fact stuck. That race is measured in *fractions of a second* — 0.081–0.084 s for codex
    (`docs/acceptance-2026-09-08-untrusted-launch.md`). A session that has been running for
    hours is not in it, and treating the two the same is what let a screenful of text end a
    session's good standing.

    It matters more than a wrong word on a row: `untrusted` is the state that carries
    **Don't trust — close it**, which DEC-078 makes *unconfirmed*. For the hour that record was
    wrong, one press would have destroyed a working session, and the pane re-read that guards
    it would have agreed, because the pane really did carry the words.

    So the RUNNING origin is bounded by the window the race actually lives in. STARTING and
    FAILED are not: those are launches that never became ready, where a late reading is the
    ordinary case rather than the suspicious one.
    """
    aged = replace(record(SessionState.RUNNING), created_at=datetime.now(UTC) - timedelta(hours=3))
    store = InMemoryStore((aged,))

    async def blocked(session_id, profile_id):
        del profile_id
        return TerminalObservation(session_id, live=True, preserved=False, awaiting_trust=True)

    service = ReconciliationService(store, settle_after=timedelta(0), confirm_ready=blocked)

    await service.reconcile((TerminalObservation(aged.session_id, live=True, preserved=False),))

    assert store.records[aged.session_id].state is SessionState.RUNNING, (
        "a session that had been running for three hours was moved to untrusted because its "
        "screen carried the words of a dialog it never drew"
    )
    assert store.events == []


async def test_no_origin_is_re_labelled_from_an_aged_screen_including_the_failed_one() -> None:
    """**The exemption I wrote for STARTING and FAILED was wrong, and this diff proves it.**

    It read: those are launches that never came up, so there is no "it was working and now it
    is not" to protect. A review pointed at the *other* commit in this same change to refute
    it. opencode's readiness marker was three ASCII dots where the agent draws an ellipsis, so
    every opencode launch burned its whole startup budget and landed in **FAILED** — with a
    live pane, running a perfectly good agent. A FAILED record is not proof that nothing is
    working; it is proof that nothing was *seen* working, and a readiness marker is exactly the
    kind of thing that can be wrong for months without anyone noticing.

    So a FAILED session with a live pane can hold real work, and leaving its correction
    unbounded left the DEC-080 hazard open through a second door: an aged screen carrying the
    dialog's words moves it to `untrusted`, which carries DEC-078's *unconfirmed* kill. The
    window is the same for all three origins now, and the argument is the same for all three:
    the race is sub-second, the first pass that can notice it is 60 s away, and anything hours
    later is likelier to be a screen than a question.
    """
    for state in (SessionState.STARTING, SessionState.FAILED, SessionState.RUNNING):
        aged = replace(record(state), created_at=datetime.now(UTC) - timedelta(hours=3))
        store = InMemoryStore((aged,))

        async def blocked(session_id, profile_id):
            del profile_id
            return TerminalObservation(session_id, live=True, preserved=False, awaiting_trust=True)

        service = ReconciliationService(store, settle_after=timedelta(0), confirm_ready=blocked)

        await service.reconcile(
            (TerminalObservation(aged.session_id, live=True, preserved=False),)
        )

        assert store.records[aged.session_id].state is state, (
            f"a {state.value} session three hours old was moved to untrusted from what its "
            "screen showed, which is the unconfirmed-kill hazard through another origin"
        )


async def test_every_origin_is_still_corrected_inside_the_window() -> None:
    """The bound narrows *when*, not *which*: all three origins still work while it is fresh."""
    for state in (SessionState.STARTING, SessionState.FAILED):
        fresh = record(state)
        store = InMemoryStore((fresh,))

        async def blocked(session_id, profile_id):
            del profile_id
            return TerminalObservation(session_id, live=True, preserved=False, awaiting_trust=True)

        service = ReconciliationService(store, settle_after=timedelta(0), confirm_ready=blocked)

        await service.reconcile(
            (TerminalObservation(fresh.session_id, live=True, preserved=False),)
        )

        assert store.records[fresh.session_id].state is SessionState.UNTRUSTED, state
        assert store.events == [LifecycleEvent.TRUST_REQUIRED], state


async def test_a_trust_blocked_pane_is_never_promoted_to_running() -> None:
    """The regression `confirm_ready` newly reporting a blocked pane as live could cause.

    `_is_ready` read `.live` alone, and a trust-blocked observation is deliberately live now
    -- the pane really is up. Reading liveness alone would therefore promote exactly the
    session that must not be promoted, which is the 2026-08-14 incident coming back through
    the change that was supposed to fix it.
    """
    failed = record(SessionState.FAILED)
    store = InMemoryStore((failed,))

    async def blocked(session_id, profile_id):
        del profile_id
        return TerminalObservation(session_id, live=True, preserved=False, awaiting_trust=True)

    service = ReconciliationService(store, settle_after=timedelta(0), confirm_ready=blocked)

    await service.reconcile((TerminalObservation(failed.session_id, live=True, preserved=False),))

    assert store.records[failed.session_id].state is SessionState.UNTRUSTED
    assert LifecycleEvent.READY not in store.events


async def test_an_untrusted_record_whose_pane_became_ready_is_promoted() -> None:
    """Answered at the keyboard, in the pane the console displays (DEC-047)."""
    untrusted = record(SessionState.UNTRUSTED)
    store = InMemoryStore((untrusted,))

    async def ready(session_id, profile_id):
        del profile_id
        return TerminalObservation(session_id, live=True, preserved=False)

    service = ReconciliationService(store, settle_after=timedelta(0), confirm_ready=ready)

    await service.reconcile(
        (TerminalObservation(untrusted.session_id, live=True, preserved=False),)
    )

    assert store.records[untrusted.session_id].state is SessionState.RUNNING
    assert store.events == [LifecycleEvent.READY]


async def test_an_untrusted_record_whose_pane_is_gone_lands_in_failed() -> None:
    """An agent that quit at its own dialog never started; ENDED would claim it ran.

    The matrix offers UNTRUSTED -> ENDED only via TRUST_DECLINED, which is this service
    answering the dialog. A pane that simply vanished was not answered by anything here, so
    the honest event is the one that says the launch never got going.
    """
    untrusted = record(SessionState.UNTRUSTED)
    store = InMemoryStore((untrusted,))
    service = ReconciliationService(store, settle_after=timedelta(0))

    await service.reconcile(())

    assert store.records[untrusted.session_id].state is SessionState.FAILED
    assert store.events == [LifecycleEvent.STARTUP_ERROR]


async def test_an_untrusted_record_whose_dialog_still_stands_is_left_completely_alone() -> None:
    """The flap. Four passes over a still-blocked pane must write nothing at all.

    This is the gap the two tests either side of it *looked* like they covered and did not:
    one uses a FAILED record, the other an UNTRUSTED record whose dialog has cleared. Neither
    is an UNTRUSTED record that is still blocked, which is the ordinary case — the owner has
    not answered yet — and it flapped RUNNING/UNTRUSTED on alternate passes, two durable
    events a minute, showing a green ACTIVE row that offered a graceful stop over an agent
    that had run nothing.
    """
    untrusted = record(SessionState.UNTRUSTED)
    store = InMemoryStore((untrusted,))

    async def blocked(session_id, profile_id):
        del profile_id
        return TerminalObservation(session_id, live=True, preserved=False, awaiting_trust=True)

    service = ReconciliationService(store, settle_after=timedelta(0), confirm_ready=blocked)

    for _ in range(4):
        await service.reconcile(
            (TerminalObservation(untrusted.session_id, live=True, preserved=False),)
        )

    assert store.records[untrusted.session_id].state is SessionState.UNTRUSTED
    assert store.events == []


async def test_an_untrusted_record_whose_pane_died_at_the_dialog_lands_in_failed() -> None:
    """`remain-on-exit` means the pane does not vanish — it dies in place.

    So the reason is `pane_dead`, never `terminal_missing`, and an UNTRUSTED branch written
    only for the latter never fires for the likeliest ending of all: the agent quit at its own
    question. Reproduced before the fix as four passes writing nothing, with the record left
    telling the owner an exited agent was waiting on them.
    """
    untrusted = record(SessionState.UNTRUSTED)
    store = InMemoryStore((untrusted,))
    service = ReconciliationService(store, settle_after=timedelta(0))

    await service.reconcile(
        (TerminalObservation(untrusted.session_id, live=False, preserved=True),)
    )

    assert store.records[untrusted.session_id].state is SessionState.FAILED
    assert store.events == [LifecycleEvent.STARTUP_ERROR]
