"""The rollover pass (DEC-115): one `ready` becomes one successor, and the predecessor is stopped
only after that successor accepts -- every failure on the way leaves it running."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.application.commands import GracefulStopCommand, LaunchCommand
from remote_agents.application.errors import DuplicateCommandError
from remote_agents.application.rollover import (
    ADOPTION_PATIENCE,
    NOT_IDLE_PATIENCE,
    RolloverPass,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.rollover import RolloverState
from remote_agents.ports.handoff_envelopes import HandoffEnvelope, HandoffEvent
from remote_agents.ports.rollover_store import Rollover
from remote_agents.ports.terminal import (
    GRACEFUL_TIMEOUT,
    PromptDelivery,
    PromptOutcome,
    PromptReason,
    TerminalObservation,
)

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
HANDOFF = "h-0123456789abcdef0123"
PROJECT = ProjectId("remote-agents")
CLAUDE = ProfileId("claude")
ROOT = Path("/work/remote-agents")
TEMPLATE = f"/planning:executing-plans --adopt-handoff {HANDOFF}"
SENT = PromptDelivery(PromptOutcome.SENT)


def _record(
    session_id: SessionId, state: SessionState = SessionState.RUNNING, profile=CLAUDE
) -> SessionRecord:
    return SessionRecord(
        session_id,
        PROJECT,
        profile,
        SessionDisplayIdentity("remote-agents", str(profile), "regular", 1, None),
        state,
        NOW,
    )


def _envelope(
    event: HandoffEvent, session_id: SessionId | str, *, plan="/abs/plan.md", code=None
) -> HandoffEnvelope:
    return HandoffEnvelope(event, HANDOFF, str(session_id), NOW, plan, code)


@dataclass
class Envelopes:
    """The project's handoff directory, in memory."""

    files: list[HandoffEnvelope] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    request: str | None = None
    discarded: list[str] = field(default_factory=list)

    def events(self, project_dir: Path) -> tuple[HandoffEnvelope, ...]:
        assert project_dir == ROOT
        return tuple(self.files)

    def write_request(self, project_dir: Path, managed_session_id: str) -> bool:
        self.requests.append(managed_session_id)
        self.request = managed_session_id
        return True

    def discard(self, project_dir: Path, handoff_id: str) -> None:
        self.discarded.append(handoff_id)
        self.files = [e for e in self.files if e.handoff_id != handoff_id]

    def clear_request(self, project_dir: Path, managed_session_id: str) -> None:
        if self.request == managed_session_id:
            self.request = None


@dataclass
class Rig:
    """Fakes for everything the pass is handed, recording what it asked of each."""

    store: SQLiteRolloverStore
    predecessor: SessionId = field(default_factory=SessionId.new)
    envelopes: Envelopes = field(default_factory=Envelopes)
    sessions: dict[SessionId, SessionRecord] = field(default_factory=dict)
    switch: bool = True
    not_idle: set[SessionId] = field(default_factory=set)
    launches: list[LaunchCommand] = field(default_factory=list)
    launch_error: Exception | None = None
    launched_state: SessionState = SessionState.RUNNING
    launch_answers_nothing: bool = False
    sends: list[tuple[SessionId, str]] = field(default_factory=list)
    deliveries: list[PromptDelivery] = field(default_factory=list)
    stops: list[GracefulStopCommand] = field(default_factory=list)
    stop_answer: str = ""
    idle_error: SessionId | None = None
    clock: list[datetime] = field(default_factory=lambda: [NOW])
    out_of_order: list[str] = field(default_factory=list)
    """Each action taken before the state licensing it was persisted; must stay empty."""

    def __post_init__(self) -> None:
        self.sessions[self.predecessor] = _record(self.predecessor)

    @property
    def successor(self) -> SessionId:
        (launched,) = [s for s in self.sessions if s != self.predecessor]
        return launched

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return tuple(self.sessions.values())

    async def enabled(self) -> bool:
        return self.switch

    def _persisted(self, **match: object) -> Rollover | None:
        """The stored row whose fields equal `match`, read at the moment of the call."""
        rows = [
            row
            for row in self.store._select("1 = 1", ())
            if all(getattr(row, name) == value for name, value in match.items())
        ]
        return rows[0] if len(rows) == 1 else None

    async def idle(self, session_id: SessionId) -> bool:
        if session_id == self.idle_error:
            raise OSError("tmux went away")
        return session_id not in self.not_idle

    async def launch(self, command: LaunchCommand) -> object:
        self.launches.append(command)
        persisted = self._persisted(handoff_id=command.idempotency_key.removeprefix("rollover:"))
        if persisted is None or persisted.state is not RolloverState.SUCCESSOR_STARTING:
            self.out_of_order.append("launch")
        if self.launch_error is not None:
            raise self.launch_error
        if self.launch_answers_nothing:
            return object()
        record = _record(SessionId.new(), self.launched_state)
        self.sessions[record.session_id] = record
        return _Launched(record)

    async def send(self, session_id: SessionId, text: str) -> PromptDelivery:
        self.sends.append((session_id, text))
        if self._persisted(successor_session_id=session_id) is None:
            self.out_of_order.append("send")
        return self.deliveries.pop(0) if self.deliveries else SENT

    async def graceful_stop(self, command: GracefulStopCommand) -> TerminalObservation:
        self.stops.append(command)
        persisted = self._persisted(
            predecessor_session_id=command.session_id, state=RolloverState.PREDECESSOR_STOPPING
        )
        if persisted is None:
            self.out_of_order.append("graceful_stop")
        if self.stop_answer:
            return TerminalObservation(command.session_id, True, False, self.stop_answer)
        self.sessions[command.session_id] = _record(command.session_id, SessionState.ENDED)
        return TerminalObservation(command.session_id, False, True)

    async def sleep(self, seconds: float) -> None:
        self.clock[0] += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    def later(self, delta: timedelta) -> None:
        self.clock[0] += delta

    def pass_(self) -> RolloverPass:
        return RolloverPass(
            self.store,
            self.envelopes,
            rollable=frozenset({CLAUDE}),
            sessions=self.list_sessions,
            handoff_root=lambda project: ROOT if project == PROJECT else None,
            enabled=self.enabled,
            idle=self.idle,
            launch=self.launch,
            send=self.send,
            graceful_stop=self.graceful_stop,
            now=lambda: self.clock[0],
            sleep=self.sleep,
        )

    async def run(self, passes: int = 1) -> None:
        for _ in range(passes):
            await self.pass_().run_once()

    def ready(self, **kwargs) -> None:
        self.envelopes.files.append(_envelope(HandoffEvent.READY, self.predecessor, **kwargs))

    def accepted_by(self, session_id: SessionId | str) -> None:
        self.envelopes.files.append(_envelope(HandoffEvent.ACCEPTED, session_id))

    async def rollover(self) -> Rollover:
        rows = self.store._select("1 = 1", ())
        assert len(rows) == 1, rows
        return rows[0]


@dataclass(frozen=True)
class _Launched:
    record: SessionRecord
    remote_control: bool = False


@pytest.fixture
def rig(tmp_path: Path):
    rig = Rig(SQLiteRolloverStore(open_database(tmp_path / "sessions.sqlite3")))
    yield rig
    # Every test: each action was licensed by a state already on disk when it was taken.
    assert rig.out_of_order == []


async def test_one_ready_across_five_passes_launches_exactly_once(rig: Rig) -> None:
    rig.ready()

    await rig.run(passes=5)

    (command,) = rig.launches
    assert command.idempotency_key == f"rollover:{HANDOFF}"
    assert (command.project_id, command.profile_id) == (PROJECT, CLAUDE)
    assert (await rig.rollover()).state is RolloverState.ADOPTING
    assert rig.stops == []


async def test_the_whole_rollover_stops_the_predecessor_only_after_its_successor_accepts(
    rig: Rig,
) -> None:
    rig.ready()
    await rig.run()
    assert rig.stops == []

    rig.accepted_by(rig.successor)
    await rig.run()

    assert rig.stops == [GracefulStopCommand(rig.predecessor, CLAUDE)]
    rollover = await rig.rollover()
    assert rollover.state is RolloverState.COMPLETED
    assert rollover.successor_session_id == rig.successor
    assert await rig.store.continued_as(rig.predecessor) == rig.successor
    assert HANDOFF in rig.envelopes.discarded


@pytest.mark.parametrize(
    "plan",
    [
        "/abs/plan.md",
        "/abs/x.md\n/exit\n",
        "; rm -rf / #",
        "--adopt-handoff h-ffffffffffffffffffff",
    ],
)
async def test_the_typed_text_is_the_template_whatever_the_envelope_names(
    rig: Rig, plan: str
) -> None:
    rig.ready(plan=plan)

    await rig.run()

    assert rig.sends == [(rig.successor, TEMPLATE)]
    assert rig.sends[0][1].encode() == TEMPLATE.encode()


async def test_an_unknown_predecessor_pane_launches_nothing_and_is_given_up_in_time(
    rig: Rig,
) -> None:
    rig.ready()
    rig.not_idle.add(rig.predecessor)

    await rig.run(passes=3)
    assert rig.launches == []
    assert (await rig.rollover()).state is RolloverState.HANDOFF_READY

    rig.later(NOT_IDLE_PATIENCE)
    await rig.run()

    rollover = await rig.rollover()
    assert (rollover.state, rollover.failure_code) == (RolloverState.FAILED, "predecessor-not-idle")
    assert rig.launches == [] and rig.stops == []


async def test_an_accepted_from_a_third_session_stops_nothing(rig: Rig) -> None:
    rig.ready()
    await rig.run()

    rig.accepted_by(SessionId.new())
    rig.accepted_by(rig.predecessor)
    await rig.run(passes=3)

    assert rig.stops == []
    assert (await rig.rollover()).state is RolloverState.ADOPTING


async def test_with_the_switch_off_nothing_is_written_or_launched(rig: Rig) -> None:
    rig.switch = False
    rig.ready()

    await rig.run(passes=3)

    assert rig.store._select("1 = 1", ()) == ()
    assert rig.launches == [] and rig.sends == [] and rig.stops == []


# Every way a rollover can fail: each leaves the predecessor running, never stopped.


async def _not_idle(rig: Rig) -> None:
    rig.not_idle.add(rig.predecessor)
    await rig.run()
    rig.later(NOT_IDLE_PATIENCE)


async def _predecessor_gone(rig: Rig) -> None:
    rig.sessions[rig.predecessor] = _record(rig.predecessor)
    await rig.store.open_for_ready(
        rig.predecessor, HANDOFF, project_id=PROJECT, profile_id=CLAUDE, plan=None, at=NOW
    )
    rig.envelopes.files.clear()
    rig.sessions[rig.predecessor] = _record(rig.predecessor, SessionState.PRESERVED)


async def _launch_raises(rig: Rig) -> None:
    rig.launch_error = RuntimeError("tmux went away")


async def _launch_duplicate(rig: Rig) -> None:
    rig.launch_error = DuplicateCommandError("claimed")


async def _launch_answers_nothing(rig: Rig) -> None:
    rig.launch_answers_nothing = True


async def _untrusted(rig: Rig) -> None:
    rig.launched_state = SessionState.UNTRUSTED


async def _successor_failed_to_start(rig: Rig) -> None:
    rig.launched_state = SessionState.FAILED


async def _refused(rig: Rig) -> None:
    rig.deliveries = [PromptDelivery(PromptOutcome.REFUSED, PromptReason.DIALOG)]


async def _unconfirmed(rig: Rig) -> None:
    rig.deliveries = [PromptDelivery(PromptOutcome.UNCONFIRMED, PromptReason.SUBMIT_NOT_SEEN)]


async def _never_ready(rig: Rig) -> None:
    rig.deliveries = [PromptDelivery(PromptOutcome.REFUSED, PromptReason.NOT_RUNNING)] * 100


async def _adoption_timeout(rig: Rig) -> None:
    await rig.run()
    rig.later(ADOPTION_PATIENCE)


async def _failed_envelope(rig: Rig) -> None:
    await rig.run()
    rig.envelopes.files.append(
        _envelope(HandoffEvent.FAILED, rig.successor, code="branch-mismatch")
    )


async def _successor_dies_adopting(rig: Rig) -> None:
    await rig.run()
    rig.sessions[rig.successor] = _record(rig.successor, SessionState.PRESERVED)


async def _successor_dies_after_accepting(rig: Rig) -> None:
    await rig.run()
    rig.accepted_by(rig.successor)
    successor = rig.successor
    rows = await rig.store.open_rollovers()
    await rig.store.advance(rows[0].id, RolloverState.SUCCESSOR_ACCEPTED, at=NOW)
    rig.sessions[successor] = _record(successor, SessionState.ENDED)


FAILURES = {
    "predecessor-not-idle": _not_idle,
    "predecessor-gone": _predecessor_gone,
    "launch-failed/raised": _launch_raises,
    "successor-unknown/duplicate": _launch_duplicate,
    "launch-failed/no-record": _launch_answers_nothing,
    "successor-untrusted": _untrusted,
    "successor-failed/start": _successor_failed_to_start,
    "not-typed/refused": _refused,
    "not-typed/unconfirmed": _unconfirmed,
    "not-typed/never-ready": _never_ready,
    "adoption-timeout": _adoption_timeout,
    "branch-mismatch": _failed_envelope,
    "successor-failed/adopting": _successor_dies_adopting,
    "successor-failed/accepted": _successor_dies_after_accepting,
}


@pytest.mark.parametrize("case", sorted(FAILURES))
async def test_every_failure_leaves_the_predecessor_unstopped(rig: Rig, case: str) -> None:
    rig.ready()
    await FAILURES[case](rig)

    await rig.run(passes=2)

    rollover = await rig.rollover()
    assert rollover.state is RolloverState.FAILED
    assert rollover.failure_code == case.split("/")[0]
    assert rig.stops == []
    assert rig.sessions[rig.predecessor].state in (SessionState.RUNNING, SessionState.PRESERVED)
    assert len(rig.launches) <= 1
    assert HANDOFF not in {e.handoff_id for e in rig.envelopes.files}


async def test_a_stop_that_does_not_verify_is_stop_failed_with_its_cause(rig: Rig) -> None:
    rig.stop_answer = GRACEFUL_TIMEOUT
    rig.ready()
    await rig.run()
    rig.accepted_by(rig.successor)

    await rig.run(passes=2)

    rollover = await rig.rollover()
    assert (rollover.state, rollover.failure_code) == (RolloverState.STOP_FAILED, GRACEFUL_TIMEOUT)
    assert len(rig.stops) == 1


async def test_a_busy_predecessor_is_not_stopped_until_idle(rig: Rig) -> None:
    rig.ready()
    await rig.run()
    rig.accepted_by(rig.successor)
    rig.not_idle.add(rig.predecessor)

    await rig.run(passes=2)
    assert rig.stops == []
    assert (await rig.rollover()).state is RolloverState.SUCCESSOR_ACCEPTED

    rig.not_idle.clear()
    await rig.run()
    assert len(rig.stops) == 1
    assert (await rig.rollover()).state is RolloverState.COMPLETED


async def test_the_owners_request_is_written_once_and_withdrawn_at_the_end(rig: Rig) -> None:
    await rig.store.request(rig.predecessor, project_id=PROJECT, profile_id=CLAUDE, at=NOW)

    await rig.run(passes=3)
    assert rig.envelopes.requests == [str(rig.predecessor)]
    assert rig.launches == []

    rig.ready()
    await rig.run()
    rig.accepted_by(rig.successor)
    await rig.run()

    assert (await rig.rollover()).state is RolloverState.COMPLETED
    assert rig.envelopes.request is None
    assert rig.envelopes.requests == [str(rig.predecessor)]


async def test_a_cancelled_request_is_withdrawn_by_the_next_pass(rig: Rig) -> None:
    requested = await rig.store.request(
        rig.predecessor, project_id=PROJECT, profile_id=CLAUDE, at=NOW
    )
    assert requested is not None
    await rig.run()
    assert rig.envelopes.request == str(rig.predecessor)

    await rig.store.advance(requested.id, RolloverState.CANCELLED, at=NOW)
    rig.switch = False
    await rig.run()

    assert rig.envelopes.request is None


async def test_a_launch_found_in_flight_without_a_successor_is_given_up_never_relaunched(
    rig: Rig,
) -> None:
    opened = await rig.store.open_for_ready(
        rig.predecessor, HANDOFF, project_id=PROJECT, profile_id=CLAUDE, plan=None, at=NOW
    )
    assert opened is not None
    await rig.store.advance(opened.id, RolloverState.SUCCESSOR_STARTING, at=NOW)
    rig.ready()

    await rig.run(passes=3)

    rollover = await rig.rollover()
    assert (rollover.state, rollover.failure_code) == (RolloverState.FAILED, "successor-unknown")
    assert rig.launches == [] and rig.stops == []


async def test_a_ready_for_a_session_of_another_profile_or_state_opens_nothing(rig: Rig) -> None:
    rig.sessions[rig.predecessor] = _record(rig.predecessor, profile=ProfileId("codex"))
    rig.ready()
    await rig.run()
    rig.sessions[rig.predecessor] = _record(rig.predecessor, SessionState.STOP_REQUESTED)
    await rig.run()

    assert rig.store._select("1 = 1", ()) == ()
    assert rig.launches == []


async def test_an_unconfirmed_send_is_never_typed_again(rig: Rig) -> None:
    rig.deliveries = [PromptDelivery(PromptOutcome.UNCONFIRMED, PromptReason.SUBMIT_NOT_SEEN)]
    rig.ready()

    await rig.run(passes=3)

    assert len(rig.sends) == 1
    assert (await rig.rollover()).failure_code == "not-typed"


async def test_a_successor_still_starting_is_typed_and_adopted(rig: Rig) -> None:
    rig.launched_state = SessionState.STARTING
    rig.ready()
    await rig.run()
    assert rig.sends == [(rig.successor, TEMPLATE)]

    rig.accepted_by(rig.successor)
    await rig.run()

    assert (await rig.rollover()).state is RolloverState.COMPLETED


async def test_turning_the_switch_off_mid_rollover_stops_nothing(rig: Rig) -> None:
    rig.ready()
    await rig.run()
    rig.accepted_by(rig.successor)
    rig.switch = False

    await rig.run(passes=2)
    rig.later(ADOPTION_PATIENCE + NOT_IDLE_PATIENCE)
    await rig.run()

    assert rig.stops == []
    assert (await rig.rollover()).state is RolloverState.ADOPTING


async def test_a_predecessor_gone_after_its_successor_accepted_completes_the_rollover(
    rig: Rig,
) -> None:
    rig.ready()
    await rig.run()
    rig.accepted_by(rig.successor)
    del rig.sessions[rig.predecessor]

    await rig.run()

    assert (await rig.rollover()).state is RolloverState.COMPLETED
    assert rig.stops == []


@pytest.mark.parametrize(
    "state", [SessionState.ORPHANED, SessionState.STARTING, SessionState.UNTRUSTED]
)
async def test_a_predecessor_neither_running_nor_stopped_is_handed_over_in_time(
    rig: Rig, state: SessionState
) -> None:
    rig.ready()
    await rig.run()
    rig.accepted_by(rig.successor)
    rig.sessions[rig.predecessor] = _record(rig.predecessor, state)

    await rig.run(passes=2)
    assert (await rig.rollover()).state is RolloverState.SUCCESSOR_ACCEPTED

    rig.later(NOT_IDLE_PATIENCE)
    await rig.run()

    rollover = await rig.rollover()
    assert (rollover.state, rollover.failure_code) == (
        RolloverState.STOP_FAILED,
        "predecessor-not-idle",
    )
    assert rig.stops == []


async def test_a_stop_found_in_flight_on_a_predecessor_in_between_is_handed_over_in_time(
    rig: Rig,
) -> None:
    rig.ready()
    await rig.run()
    successor = rig.successor
    (row,) = await rig.store.open_rollovers()
    await rig.store.advance(row.id, RolloverState.SUCCESSOR_ACCEPTED, at=NOW)
    await rig.store.advance(row.id, RolloverState.PREDECESSOR_STOPPING, at=NOW)
    rig.sessions[rig.predecessor] = _record(rig.predecessor, SessionState.STOP_REQUESTED)

    await rig.run()
    assert (await rig.rollover()).state is RolloverState.PREDECESSOR_STOPPING

    rig.later(NOT_IDLE_PATIENCE)
    await rig.run()

    assert (await rig.rollover()).state is RolloverState.STOP_FAILED
    assert rig.stops == [] and rig.sessions[successor].state is SessionState.RUNNING


async def test_one_rollovers_fault_does_not_hold_back_another(rig: Rig) -> None:
    other = SessionId.new()
    rig.sessions[other] = _record(other)
    for predecessor, handoff in ((rig.predecessor, HANDOFF), (other, "h-ffffffffffffffffffff")):
        opened = await rig.store.open_for_ready(
            predecessor, handoff, project_id=PROJECT, profile_id=CLAUDE, plan=None, at=NOW
        )
        assert opened is not None
    rig.idle_error = rig.predecessor

    await rig.run()

    assert [c.idempotency_key for c in rig.launches] == ["rollover:h-ffffffffffffffffffff"]
    rows = {r.predecessor_session_id: r.state for r in rig.store._select("1 = 1", ())}
    assert rows[rig.predecessor] is RolloverState.HANDOFF_READY
