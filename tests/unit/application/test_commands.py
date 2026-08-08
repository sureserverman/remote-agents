"""Use-case tests prove the sealed command surface and durable ordering."""

import asyncio
from collections.abc import Collection, Sequence
from dataclasses import replace

import pytest

from remote_agents.adapters.tmux.fake import FakeTerminal as OwnershipAwareTerminal
from remote_agents.application.commands import ForceStopCommand, GracefulStopCommand, LaunchCommand
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
        return updated

    async def claim_idempotency_key(self, key: str) -> bool:
        if key in self.claims:
            return False
        self.claims.add(key)
        return True


class FakeTerminal:
    def __init__(self, live: bool = True, *, graceful_preserved: bool = True) -> None:
        self.live = live
        self.graceful_preserved = graceful_preserved
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
            session_id, live=not self.graceful_preserved, preserved=self.graceful_preserved
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


async def test_a_stop_the_policy_refuses_also_raises_in_the_service() -> None:
    """Defence in depth: availability is presentation, the state machine is the authority.

    Written after a policy edit offered force from ORPHANED. Every surface-level test passed,
    because their fakes recorded the dispatch instead of transitioning; only the real service
    shows that the terminal is never reached.
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
        SessionState.ORPHANED,
        record.created_at,
    )

    assert "force" not in available_actions(SessionState.ORPHANED)
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
