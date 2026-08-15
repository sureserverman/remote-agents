"""Use-case tests prove the sealed command surface and durable ordering."""

import asyncio
import logging
from collections.abc import Collection, Sequence
from dataclasses import replace

import pytest

from remote_agents.adapters.tmux.fake import FakeTerminal as OwnershipAwareTerminal
from remote_agents.application.commands import (
    CleanupCommand,
    ForceStopCommand,
    GracefulStopCommand,
    LaunchCommand,
)
from remote_agents.application.errors import DuplicateCommandError
from remote_agents.application.services import SessionService
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionRecord, SessionState
from remote_agents.domain.state_machine import LifecycleEvent, transition
from remote_agents.ports.terminal import TerminalObservation


class FakeStore:
    def __init__(self) -> None:
        self.records: dict[SessionId, SessionRecord] = {}
        self.claims: set[str] = set()
        self.events: list[LifecycleEvent] = []

    async def next_sequence(self, project_id: ProjectId, profile_id: ProfileId) -> int:
        return 1

    async def save(self, record: SessionRecord) -> None:
        self.records[record.session_id] = record

    async def get(self, session_id: SessionId) -> SessionRecord | None:
        return self.records.get(session_id)

    async def list(self, states: Collection[SessionState] | None = None) -> Sequence[SessionRecord]:
        return tuple(self.records.values())

    async def record_event(self, session_id: SessionId, event: LifecycleEvent) -> SessionRecord:
        self.events.append(event)
        # `replace` rather than a positional rebuild — see the twin in test_reconcile.py: the
        # old shape dropped every field after created_at, so a fake could lose what the real
        # store carries and the test would still pass.
        current = self.records[session_id]
        updated = replace(current, state=transition(current.state, event).to_state)
        self.records[session_id] = updated
        return updated

    async def claim_idempotency_key(self, key: str) -> bool:
        if key in self.claims:
            return False
        self.claims.add(key)
        return True


class FakeTerminal:
    def __init__(
        self,
        live: bool = True,
        *,
        graceful_preserved: bool = True,
        graceful_detail: str = "",
    ) -> None:
        self.live = live
        self.graceful_preserved = graceful_preserved
        self.graceful_detail = graceful_detail
        self.launches: list[tuple[SessionId, ProjectId, ProfileId]] = []
        self.force_stop_calls = 0

    async def launch(
        self, session_id: SessionId, project_id: ProjectId, profile_id: ProfileId
    ) -> TerminalObservation:
        self.launches.append((session_id, project_id, profile_id))
        return TerminalObservation(session_id, live=self.live, preserved=False)

    async def inspect(self, session_id: SessionId) -> TerminalObservation | None:
        return TerminalObservation(session_id, live=self.live, preserved=False)

    async def confirm_ready(
        self, session_id: SessionId, _profile_id: ProfileId
    ) -> TerminalObservation:
        return TerminalObservation(session_id, live=self.live, preserved=False)

    async def graceful_stop(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation:
        return TerminalObservation(
            session_id,
            live=not self.graceful_preserved,
            preserved=self.graceful_preserved,
            detail=self.graceful_detail,
        )

    async def cleanup(self, session_id: SessionId) -> None:
        return None

    async def force_stop(self, session_id: SessionId) -> TerminalObservation:
        self.force_stop_calls += 1
        return TerminalObservation(session_id, live=False, preserved=False)


class YieldingForceStopTerminal(FakeTerminal):
    async def force_stop(self, session_id: SessionId) -> TerminalObservation:
        self.force_stop_calls += 1
        await asyncio.sleep(0)
        return TerminalObservation(session_id, live=False, preserved=False)


async def test_launch_uses_only_typed_ids_and_terminal_evidence_for_liveness() -> None:
    store = FakeStore()
    terminal = FakeTerminal(live=False)
    service = SessionService(store, terminal)

    record = await service.launch(
        LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "key-1")
    )

    assert terminal.launches[0][1:] == (ProjectId("opaque-editor"), ProfileId("claude"))
    assert record.state is SessionState.FAILED
    assert store.events == [LifecycleEvent.STARTUP_ERROR]


async def test_duplicate_launch_does_not_repeat_terminal_side_effect() -> None:
    service = SessionService(FakeStore(), FakeTerminal())
    command = LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "same")
    await service.launch(command)

    with pytest.raises(DuplicateCommandError):
        await service.launch(command)


async def test_refresh_readiness_recovers_only_a_failed_launch_with_readiness_evidence() -> None:
    terminal = FakeTerminal(live=False)
    service = SessionService(FakeStore(), terminal)
    record = await service.launch(
        LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "key")
    )

    terminal.live = True
    refreshed = await service.refresh_readiness()

    assert record.state is SessionState.FAILED
    assert refreshed[0].state is SessionState.RUNNING


async def test_graceful_stop_timeout_restores_running_state_for_explicit_force_stop() -> None:
    store = FakeStore()
    service = SessionService(store, FakeTerminal(graceful_preserved=False))
    record = await service.launch(LaunchCommand(ProjectId("opaque-editor"), ProfileId("codex"), "key"))

    observation = await service.graceful_stop(
        GracefulStopCommand(record.session_id, record.profile_id)
    )

    assert observation.preserved is False
    assert store.records[record.session_id].state is SessionState.RUNNING
    assert store.events[-2:] == [
        LifecycleEvent.GRACEFUL_STOP_REQUESTED,
        LifecycleEvent.GRACEFUL_STOP_TIMED_OUT,
    ]


async def test_a_stop_that_was_never_sent_is_not_recorded_as_a_timeout() -> None:
    """The audit log must not assert an exit sequence that was never sent (DEC-022).

    `TmuxRuntime.graceful_stop` answers `unknown_session` when this host could not match the
    session to a live pane it owns — no profile curated, no managed pane found, or a pane
    belonging to a different one. Nothing is signalled to the agent in any of those, so
    recording `GRACEFUL_STOP_TIMED_OUT` claimed a wait that never happened. The state still
    lands on RUNNING, because nothing was stopped; only the event differs.

    Existing rows are deliberately not migrated (DEC-022): the stored row records the event
    and not the observation behind it, so nothing in the database says which historical
    `GRACEFUL_STOP_TIMED_OUT` rows were real timeouts. A migration would have to guess, and
    rewriting audit history with a guess is worse than the ambiguity it replaces.
    """
    store = FakeStore()
    terminal = FakeTerminal(graceful_preserved=False, graceful_detail="unknown_session")
    service = SessionService(store, terminal)
    record = await service.launch(LaunchCommand(ProjectId("opaque-editor"), ProfileId("codex"), "key"))

    observation = await service.graceful_stop(
        GracefulStopCommand(record.session_id, record.profile_id)
    )

    assert observation.preserved is False
    assert store.records[record.session_id].state is SessionState.RUNNING
    assert store.events[-2:] == [
        LifecycleEvent.GRACEFUL_STOP_REQUESTED,
        LifecycleEvent.GRACEFUL_STOP_NEVER_SENT,
    ]


async def test_a_stop_reporting_an_unknown_cause_says_so_rather_than_defaulting_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unrecognised `detail` still records something, and does not do it silently.

    `stop_failure` already refuses the fail-dangerous reading of this exact field — "an
    unrecognised `detail` is a failure, not an unknown" — and the same reasoning applies to
    the event. The two known causes are different *claims*, so falling through to the timeout
    without a word is how a future "nothing left the host" cause gets written into the durable
    history as a wait that never happened, which is the defect DEC-022 exists to remove.

    The timeout is still what gets recorded, because it is what this method recorded for every
    cause before DEC-022 — the fallback adds no claim that was not already there. What is new
    is that it is visible. Found by the Stage 2 gate evaluator.
    """
    store = FakeStore()
    terminal = FakeTerminal(graceful_preserved=False, graceful_detail="something_nobody_added_yet")
    service = SessionService(store, terminal)
    record = await service.launch(LaunchCommand(ProjectId("opaque-editor"), ProfileId("codex"), "key"))

    with caplog.at_level(logging.WARNING):
        await service.graceful_stop(GracefulStopCommand(record.session_id, record.profile_id))

    assert store.events[-1] is LifecycleEvent.GRACEFUL_STOP_TIMED_OUT
    assert "something_nobody_added_yet" in caplog.text, (
        "an unrecognised cause was recorded as a timeout with nothing said about it"
    )


async def test_concurrent_force_stops_allow_only_one_terminal_side_effect() -> None:
    terminal = YieldingForceStopTerminal()
    service = SessionService(FakeStore(), terminal)
    record = await service.launch(
        LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "one")
    )

    results = await asyncio.gather(
        service.force_stop(ForceStopCommand(record.session_id)),
        service.force_stop(ForceStopCommand(record.session_id)),
        return_exceptions=True,
    )

    assert terminal.force_stop_calls == 1
    assert sum(isinstance(result, Exception) for result in results) == 1


@pytest.mark.parametrize("provenance", [None, "ambiguous"])
async def test_a_stop_the_policy_refuses_also_raises_in_the_service(provenance) -> None:
    """Defence in depth: availability is presentation, and something else must be authority.

    Written after a policy edit offered force from ORPHANED. Every surface-level test passed,
    because their fakes recorded the dispatch instead of transitioning; only the real service
    showed that the terminal was never reached.

    **What the authority is changed under DEC-020, and that is the whole point of this test
    now.** It used to be the state machine: availability narrowed the domain on `SessionState`
    alone, so anything the policy refused the matrix refused too. DEC-020 gives ORPHANED one
    outgoing transition and branches the *policy* on `orphan_provenance` — a record field the
    matrix, a pure function of state, cannot read. So the matrix stopped being able to make
    this refusal, and for one commit nothing else made it either.

    `SessionService.force_stop` now asks `available_actions` directly for the ORPHANED case.
    Both conservative provenances are driven here because they are different facts — an
    unreadable or pre-migration-6 row, and a positively-ambiguous one — that must not diverge.
    """
    from remote_agents.application.errors import StopNotPermittedError
    from remote_agents.application.session_actions import available_actions
    from remote_agents.domain.models import OrphanProvenance

    resolved = OrphanProvenance.AMBIGUOUS if provenance == "ambiguous" else None
    store = FakeStore()
    terminal = FakeTerminal()
    service = SessionService(store, terminal)
    record = await service.launch(
        LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "one")
    )
    store.records[record.session_id] = SessionRecord(
        record.session_id,
        record.project_id,
        record.profile_id,
        record.display,
        SessionState.ORPHANED,
        record.created_at,
        orphan_provenance=resolved,
    )

    assert "force" not in available_actions(SessionState.ORPHANED, resolved)
    with pytest.raises(StopNotPermittedError):
        await service.force_stop(ForceStopCommand(record.session_id))
    assert terminal.force_stop_calls == 0


@pytest.mark.parametrize("state", [SessionState.RUNNING, SessionState.STOP_REQUESTED])
async def test_cleanup_is_refused_from_a_state_the_policy_never_offers_it_from(
    state: SessionState,
) -> None:
    """The gap a false premise had been hiding, and it predates DEC-020 entirely.

    `CLEANUP_CONFIRMED` is domain-legal from RUNNING and STOP_REQUESTED, while
    `available_actions` offers cleanup only from PRESERVED. So for these two states the
    matrix would walk a live session to ENDED and ask the terminal to kill its tmux session,
    on an action no surface offers and no confirmation guards. Both surfaces check the policy
    before calling, so an owner could not reach it — it was simply undefended at the layer
    that owns the action.

    Found by the Stage 4 gate's adversarial pass, which noticed that the sibling guard added
    to `force_stop` had been justified by a docstring claiming no such gap existed. The
    architecture test asserting the policy is *narrower* than the domain had been asserting
    the opposite of that claim all along.
    """
    from remote_agents.application.errors import StopNotPermittedError
    from remote_agents.application.session_actions import CLEANUP, available_actions
    from remote_agents.domain.state_machine import LifecycleEvent, transition

    assert CLEANUP not in available_actions(state, None)
    assert transition(state, LifecycleEvent.CLEANUP_CONFIRMED).to_state is SessionState.ENDED, (
        "the premise: the domain would allow this, so only the policy can refuse it"
    )

    store = FakeStore()
    terminal = FakeTerminal()
    service = SessionService(store, terminal)
    record = await service.launch(
        LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "one")
    )
    store.records[record.session_id] = SessionRecord(
        record.session_id,
        record.project_id,
        record.profile_id,
        record.display,
        state,
        record.created_at,
    )

    with pytest.raises(StopNotPermittedError):
        await service.cleanup(CleanupCommand(record.session_id))

    assert store.records[record.session_id].state is state


async def test_cleanup_still_works_from_the_state_the_policy_does_offer_it_from() -> None:
    """So the guard above cannot be tightened into refusing the only case that matters."""
    from remote_agents.application.session_actions import CLEANUP, available_actions

    assert CLEANUP in available_actions(SessionState.PRESERVED, None)

    store = FakeStore()
    terminal = FakeTerminal()
    service = SessionService(store, terminal)
    record = await service.launch(
        LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "one")
    )
    store.records[record.session_id] = SessionRecord(
        record.session_id,
        record.project_id,
        record.profile_id,
        record.display,
        SessionState.PRESERVED,
        record.created_at,
    )

    await service.cleanup(CleanupCommand(record.session_id))

    assert store.records[record.session_id].state is SessionState.ENDED


async def test_the_service_lets_a_force_reach_an_adopted_orphan() -> None:
    """The other side of the guard above, so it cannot be tightened into refusing everything.

    A backstop that refused both branches would pass every test asserting a refusal and
    silently delete the capability DEC-020 exists to add.
    """
    from remote_agents.domain.models import OrphanProvenance

    store = FakeStore()
    terminal = FakeTerminal()
    service = SessionService(store, terminal)
    record = await service.launch(
        LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "one")
    )
    store.records[record.session_id] = SessionRecord(
        record.session_id,
        record.project_id,
        record.profile_id,
        record.display,
        SessionState.ORPHANED,
        record.created_at,
        orphan_provenance=OrphanProvenance.ADOPTED,
    )

    await service.force_stop(ForceStopCommand(record.session_id))

    assert terminal.force_stop_calls == 1
    assert store.records[record.session_id].state is SessionState.ENDED


async def test_the_service_still_refuses_a_force_from_a_state_with_no_transition() -> None:
    """The half of the double guard that survives, kept so the whole idea is not lost.

    STARTING offers no force and the matrix permits none from it, so for every state but
    ORPHANED the service is still the backstop it always was.
    """
    from remote_agents.application.session_actions import available_actions
    from remote_agents.domain.state_machine import InvalidTransition

    store = FakeStore()
    terminal = FakeTerminal()
    service = SessionService(store, terminal)
    record = await service.launch(
        LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "one")
    )
    store.records[record.session_id] = SessionRecord(
        record.session_id,
        record.project_id,
        record.profile_id,
        record.display,
        SessionState.STARTING,
        record.created_at,
    )

    assert "force" not in available_actions(SessionState.STARTING, None)
    with pytest.raises(InvalidTransition):
        await service.force_stop(ForceStopCommand(record.session_id))
    assert terminal.force_stop_calls == 0


async def test_copy_attach_refuses_a_pane_that_belongs_to_another_project() -> None:
    """The ownership half of copy_attach's guard, exercised rather than merely reachable.

    FakeTerminal records project and profile on launch precisely so this branch can be
    driven; without that, every fake observation was unowned and this refusal was dead.
    """
    store = FakeStore()
    terminal = OwnershipAwareTerminal()
    service = SessionService(store, terminal)
    record = await service.launch(
        LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "one")
    )

    assert await service.copy_attach(record.session_id) is not None

    # The same pane, now reporting a different project than the record carries.
    observation = await terminal.inspect(record.session_id)
    terminal._observations[record.session_id] = replace(
        observation, project_id=ProjectId("someone-elses-project")
    )

    assert await service.copy_attach(record.session_id) is None


async def test_copy_attach_offers_a_preserved_pane_read_only() -> None:
    """DEC-021: PRESERVED gets its output back, and gets it read-only.

    The refusal this replaces read as though tmux forbade attaching to a preserved pane. It
    does not — the pane is still there, holding exactly the output PRESERVED exists to keep,
    and refusing to show it made the state less useful than the thing it replaced. What the
    agent's exit does remove is anything to type *to*, which is why the form is `-r`.
    """
    store = FakeStore()
    terminal = OwnershipAwareTerminal()
    service = SessionService(store, terminal)
    record = await service.launch(
        LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "one")
    )
    observation = await terminal.inspect(record.session_id)
    terminal._observations[record.session_id] = replace(observation, live=False, preserved=True)

    command = await service.copy_attach(record.session_id)

    assert command is not None, "a preserved pane still has the output PRESERVED exists to keep"
    assert "attach-session -r -t" in command, f"the offer must be read-only, but it was {command!r}"


async def test_the_fake_carries_ownership_across_a_preserving_stop() -> None:
    """The fake's preserved observation names its owner, as the real runtime's does.

    Ownership is what `copy_attach` compares against the record, so a fake that blanked it on
    the transition modelled a terminal whose preserved panes have no owner — and the real one
    does keep them: a dead pane still answers `parse_pane` with its `@remote_agents_*` session
    options, verified against tmux 3.4 during the Stage 3 gate.

    **This does not assert that a gracefully stopped session is attachable, and an earlier
    draft did — wrongly, on a premise a reviewer and I both accepted without checking.**
    `SessionService.graceful_stop` calls `terminal.cleanup` immediately after `PANE_EXITED`,
    so the pane is removed and the record reaches ENDED; there is nothing left to attach to.
    PRESERVED as an *attachable* state comes from reconciliation finding a dead pane
    (`RECONCILED_PANE_DEAD`), which is why the DEC-021 tests above build that observation
    directly rather than by driving a stop. What is pinned here is the fidelity of the
    transition itself, which is the thing that was actually wrong.
    """
    terminal = OwnershipAwareTerminal()
    service = SessionService(FakeStore(), terminal)
    record = await service.launch(
        LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "one")
    )

    observation = await service.graceful_stop(
        GracefulStopCommand(record.session_id, record.profile_id)
    )

    assert observation.preserved
    assert observation.project_id == record.project_id, (
        "the preserved observation lost its project, so an ownership check against the record "
        "would refuse a pane the record owns"
    )
    assert observation.profile_id == record.profile_id


async def test_copy_attach_still_refuses_a_pane_that_is_neither_live_nor_preserved() -> None:
    """The half the relaxation must not take with it.

    Widening the gate to accept `preserved` is one step from accepting anything that is not
    `None`, and an observation that is neither live nor preserved is a pane whose evidence is
    ambiguous — the ORPHANED producer. Handing over an attach command for it would be
    inventing a pane.
    """
    store = FakeStore()
    terminal = OwnershipAwareTerminal()
    service = SessionService(store, terminal)
    record = await service.launch(
        LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "one")
    )
    observation = await terminal.inspect(record.session_id)
    terminal._observations[record.session_id] = replace(observation, live=False, preserved=False)

    assert await service.copy_attach(record.session_id) is None


async def test_copy_attach_refuses_a_pane_running_another_profile() -> None:
    store = FakeStore()
    terminal = OwnershipAwareTerminal()
    service = SessionService(store, terminal)
    record = await service.launch(
        LaunchCommand(ProjectId("opaque-editor"), ProfileId("claude"), "one")
    )
    observation = await terminal.inspect(record.session_id)
    terminal._observations[record.session_id] = replace(observation, profile_id=ProfileId("codex"))

    assert await service.copy_attach(record.session_id) is None
