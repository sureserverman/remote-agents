"""Restart recovery (DEC-115, brief §26): a rollover persisted at any state, its pass dropped as
a crash between persist and action would drop it, and a new pass built on the same database.
The new pass follows `recovery_action`, never launches a second successor, never relaunches a
launch it cannot account for, leaves the predecessor running on every failure, and records the
recovery as its own history row."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.application.commands import GracefulStopCommand, LaunchCommand
from remote_agents.application.rollover import ADOPTION_PATIENCE, RolloverPass
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.rollover import (
    TERMINAL,
    RecoveryAction,
    RecoveryFacts,
    RolloverState,
    recovery_action,
)
from remote_agents.ports.handoff_envelopes import HandoffEnvelope, HandoffEvent
from remote_agents.ports.terminal import PromptDelivery, PromptOutcome, TerminalObservation

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
RESTART = NOW + timedelta(minutes=1)
HANDOFF = "h-0123456789abcdef0123"
PROJECT = ProjectId("remote-agents")
CLAUDE = ProfileId("claude")
ROOT = Path("/work/remote-agents")
S = RolloverState


def _record(session_id: SessionId, state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        session_id,
        PROJECT,
        CLAUDE,
        SessionDisplayIdentity("remote-agents", "claude", "regular", 1, None),
        state,
        NOW,
    )


@dataclass
class Envelopes:
    files: list[HandoffEnvelope] = field(default_factory=list)
    request: str | None = None
    requests: int = 0

    def events(self, project_dir: Path) -> tuple[HandoffEnvelope, ...]:
        return tuple(self.files)

    def write_request(self, project_dir: Path, managed_session_id: str) -> bool:
        self.requests += 1
        self.request = managed_session_id
        return True

    def discard(self, project_dir: Path, handoff_id: str) -> None:
        self.files = [e for e in self.files if e.handoff_id != handoff_id]

    def clear_request(self, project_dir: Path, managed_session_id: str) -> None:
        if self.request == managed_session_id:
            self.request = None


@dataclass
class Host:
    """The fake terminal and session list both passes share, as one `serve` host would."""

    predecessor: SessionId = field(default_factory=SessionId.new)
    sessions: dict[SessionId, SessionRecord] = field(default_factory=dict)
    envelopes: Envelopes = field(default_factory=Envelopes)
    launches: list[LaunchCommand] = field(default_factory=list)
    sends: list[SessionId] = field(default_factory=list)
    stops: list[GracefulStopCommand] = field(default_factory=list)
    clock: list[datetime] = field(default_factory=lambda: [NOW])

    def __post_init__(self) -> None:
        self.sessions[self.predecessor] = _record(self.predecessor)

    def add_successor(self, state: SessionState = SessionState.RUNNING) -> SessionId:
        successor = SessionId.new()
        self.sessions[successor] = _record(successor, state)
        return successor

    def successors(self) -> list[SessionId]:
        return [s for s in self.sessions if s != self.predecessor]

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return tuple(self.sessions.values())

    async def enabled(self) -> bool:
        return True

    async def idle(self, session_id: SessionId) -> bool:
        return True

    async def launch(self, command: LaunchCommand) -> object:
        self.launches.append(command)
        return _Launched(self.sessions[self.add_successor()])

    async def send(self, session_id: SessionId, text: str) -> PromptDelivery:
        self.sends.append(session_id)
        return PromptDelivery(PromptOutcome.SENT)

    async def graceful_stop(self, command: GracefulStopCommand) -> TerminalObservation:
        self.stops.append(command)
        self.sessions[command.session_id] = _record(command.session_id, SessionState.ENDED)
        return TerminalObservation(command.session_id, False, True)

    async def sleep(self, seconds: float) -> None:
        self.clock[0] += timedelta(seconds=seconds)

    def pass_on(self, store: SQLiteRolloverStore) -> RolloverPass:
        return RolloverPass(
            store,
            self.envelopes,
            rollable=frozenset({CLAUDE}),
            sessions=self.list_sessions,
            handoff_root=lambda project: ROOT,
            enabled=self.enabled,
            idle=self.idle,
            launch=self.launch,
            send=self.send,
            graceful_stop=self.graceful_stop,
            now=lambda: self.clock[0],
            sleep=self.sleep,
        )


@dataclass(frozen=True)
class _Launched:
    record: SessionRecord
    remote_control: bool = False


@dataclass(frozen=True)
class Case:
    """A rollover left at `state`, with what the host shows when the new pass comes up."""

    state: RolloverState
    successor: SessionState | None = None
    """The recorded successor's state; None records no successor."""
    predecessor: SessionState = SessionState.RUNNING
    timed_out: bool = False
    accepted: bool = False

    @property
    def name(self) -> str:
        parts = [self.state.name]
        if self.successor is not None:
            parts.append(f"successor-{self.successor.value}")
        if self.predecessor is not SessionState.RUNNING:
            parts.append(f"predecessor-{self.predecessor.value}")
        if self.timed_out:
            parts.append("timed-out")
        if self.accepted:
            parts.append("accepted")
        return ":".join(parts)


CASES = [
    Case(S.REQUESTED),
    Case(S.HANDOFF_READY),
    Case(S.SUCCESSOR_STARTING),
    Case(S.SUCCESSOR_STARTING, successor=SessionState.RUNNING),
    Case(S.SUCCESSOR_STARTING, successor=SessionState.ENDED),
    Case(S.ADOPTING, successor=SessionState.RUNNING),
    Case(S.ADOPTING, successor=SessionState.RUNNING, accepted=True),
    Case(S.ADOPTING, successor=SessionState.RUNNING, timed_out=True),
    Case(S.SUCCESSOR_ACCEPTED, successor=SessionState.RUNNING),
    Case(S.SUCCESSOR_ACCEPTED, successor=SessionState.ENDED),
    Case(S.PREDECESSOR_STOPPING, successor=SessionState.RUNNING),
    Case(
        S.PREDECESSOR_STOPPING,
        successor=SessionState.RUNNING,
        predecessor=SessionState.ENDED,
    ),
]

_PATH = {
    S.HANDOFF_READY: (),
    S.SUCCESSOR_STARTING: (S.SUCCESSOR_STARTING,),
    S.ADOPTING: (S.SUCCESSOR_STARTING, S.ADOPTING),
    S.SUCCESSOR_ACCEPTED: (S.SUCCESSOR_STARTING, S.ADOPTING, S.SUCCESSOR_ACCEPTED),
    S.PREDECESSOR_STOPPING: (
        S.SUCCESSOR_STARTING,
        S.ADOPTING,
        S.SUCCESSOR_ACCEPTED,
        S.PREDECESSOR_STOPPING,
    ),
}


async def _persist(store: SQLiteRolloverStore, host: Host, case: Case) -> str:
    """Write the rollover the crashed pass left, through the store as that pass would have."""
    if case.state is S.REQUESTED:
        row = await store.request(host.predecessor, project_id=PROJECT, profile_id=CLAUDE, at=NOW)
        assert row is not None
        return row.id
    host.envelopes.files.append(
        HandoffEnvelope(HandoffEvent.READY, HANDOFF, str(host.predecessor), NOW, "/p.md", None)
    )
    row = await store.open_for_ready(
        host.predecessor, HANDOFF, project_id=PROJECT, profile_id=CLAUDE, plan="/p.md", at=NOW
    )
    assert row is not None
    successor = None if case.successor is None else host.add_successor(case.successor)
    for state in _PATH[case.state]:
        await store.advance(row.id, state, at=NOW)
        if state is S.SUCCESSOR_STARTING and successor is not None:
            await store.record_successor(row.id, successor, at=NOW)
    if case.accepted:
        assert successor is not None
        host.envelopes.files.append(
            HandoffEnvelope(HandoffEvent.ACCEPTED, HANDOFF, str(successor), NOW, "/p.md", None)
        )
    host.sessions[host.predecessor] = _record(host.predecessor, case.predecessor)
    return row.id


def _facts(case: Case) -> RecoveryFacts:
    return RecoveryFacts(
        has_successor_id=case.successor is not None,
        successor_alive=case.successor in (SessionState.RUNNING, SessionState.STARTING),
        timed_out=case.timed_out,
    )


async def _quiesce(store: SQLiteRolloverStore, host: Host, rollover_id: str) -> None:
    """Run the new pass until a pass writes nothing more (or ten passes have run)."""
    recovered = host.pass_on(store)
    for _ in range(10):
        before = len(await store.events(rollover_id))
        await recovered.run_once()
        if len(await store.events(rollover_id)) == before:
            return


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_a_restart_at_every_state_recovers_by_the_table(tmp_path: Path, case: Case) -> None:
    database = tmp_path / "sessions.sqlite3"
    host = Host()
    rollover_id = await _persist(SQLiteRolloverStore(open_database(database)), host, case)
    launched_before = len(host.launches)
    successors_before = len(host.successors())

    # The crashed pass is gone; a new one opens the same database after the restart.
    host.clock[0] = RESTART + (ADOPTION_PATIENCE if case.timed_out else timedelta())
    store = SQLiteRolloverStore(open_database(database))
    await _quiesce(store, host, rollover_id)

    row = await store.get(rollover_id)
    assert row is not None
    action = recovery_action(case.state, _facts(case))

    # At most one successor, ever, and no launch unless the table says launch.
    assert len(host.successors()) <= 1
    if action is not RecoveryAction.LAUNCH:
        assert len(host.launches) == launched_before
        assert len(host.successors()) == successors_before
    if case.state is S.SUCCESSOR_STARTING and case.successor is None:
        assert (row.state, row.failure_code) == (S.FAILED, "successor-unknown")
        assert host.launches == []
    # The template is never typed again into a successor a crashed pass already launched.
    if case.successor is not None:
        assert host.sends == []

    # A FAILED rollover leaves its predecessor exactly as it found it, and stops nothing.
    if row.state is S.FAILED:
        assert host.stops == []
        assert host.sessions[host.predecessor].state is case.predecessor

    # The recovery is its own row, written by the new pass, naming the table's action.
    events = await store.events(rollover_id)
    recovery = [e for e in events if e.detail == f"restart: {action.value}"]
    assert len(recovery) == 1
    assert recovery[0].from_state is case.state and recovery[0].to_state is case.state
    assert recovery[0].created_at >= RESTART


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_the_first_move_after_a_restart_is_the_tables(tmp_path: Path, case: Case) -> None:
    database = tmp_path / "sessions.sqlite3"
    host = Host()
    rollover_id = await _persist(SQLiteRolloverStore(open_database(database)), host, case)
    host.clock[0] = RESTART + (ADOPTION_PATIENCE if case.timed_out else timedelta())
    store = SQLiteRolloverStore(open_database(database))

    await host.pass_on(store).run_once()

    events = await store.events(rollover_id)
    after = [e for e in events if e.created_at >= RESTART and e.detail != _detail(case)]
    moves = [e for e in after if e.from_state is not e.to_state]
    action = recovery_action(case.state, _facts(case))
    if action is RecoveryAction.WAIT:
        # Waiting is the ordinary step and nothing recovery-specific: no launch, and no give-up.
        # A live successor found starting is waited on in ADOPTING, whose patience bounds it.
        assert host.launches == []
        assert all(move.to_state is not S.FAILED for move in moves)
    else:
        assert moves, f"{action} made no move"
        assert (moves[0].from_state, moves[0].to_state) == (case.state, _first_move(action, case))


def _detail(case: Case) -> str:
    return f"restart: {recovery_action(case.state, _facts(case)).value}"


def _first_move(action: RecoveryAction, case: Case) -> RolloverState:
    if action is RecoveryAction.RECONCILE_STOP:
        stopped = case.predecessor in (SessionState.ENDED, SessionState.PRESERVED)
        return S.COMPLETED if stopped else S.STOP_FAILED
    return {
        RecoveryAction.LAUNCH: S.SUCCESSOR_STARTING,
        RecoveryAction.GIVE_UP_SUCCESSOR_UNKNOWN: S.FAILED,
        RecoveryAction.GIVE_UP_SUCCESSOR_FAILED: S.FAILED,
        RecoveryAction.TIME_OUT: S.FAILED,
        RecoveryAction.STOP_PREDECESSOR: S.PREDECESSOR_STOPPING,
    }[action]


def test_every_non_terminal_state_has_a_case() -> None:
    assert {case.state for case in CASES} == set(RolloverState) - TERMINAL
