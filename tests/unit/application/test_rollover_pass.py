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
from remote_agents.application.commands import (
    UNATTENDED_LAUNCH_WAIT_SECONDS,
    GracefulStopCommand,
    LaunchCommand,
)
from remote_agents.application.errors import DuplicateCommandError
from remote_agents.application.reconcile import SessionLocks
from remote_agents.application.rollover import (
    ADOPTION_PATIENCE,
    NOT_IDLE_PATIENCE,
    READY_FRESHNESS,
    TRUST_HOLD,
    TRUST_PATIENCE,
    TRUSTED_WAITING,
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
from remote_agents.ports.rollover_store import Rollover, RolloverEvent
from remote_agents.ports.terminal import (
    GRACEFUL_TIMEOUT,
    NOT_IDLE,
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
CODEX = ProfileId("codex")
TEMPLATES = {
    CLAUDE: "/planning:executing-plans --adopt-handoff {handoff_id}",
    CODEX: "$planning:executing-plans --adopt-handoff {handoff_id}",
}
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
    discard_error: bool = False

    def events(self, project_dir: Path) -> tuple[HandoffEnvelope, ...]:
        assert project_dir == ROOT
        if self.events_error:
            raise OSError("the handoff directory could not be read")
        return tuple(self.files)

    refuse_writes: bool = False
    events_error: bool = False

    def write_request(self, project_dir: Path, managed_session_id: str) -> bool:
        if self.refuse_writes:
            return False
        self.requests.append(managed_session_id)
        self.request = managed_session_id
        return True

    def requested(self, project_dir: Path) -> str | None:
        return self.request

    def discard(self, project_dir: Path, handoff_id: str) -> None:
        if self.discard_error:
            raise OSError("the handoff directory went read-only")
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
    send_states: list[RolloverState | None] = field(default_factory=list)
    """The successor's rollover state on disk at each send."""
    send_crashes: bool = False
    """The next send is cut short as a killed process cuts it: after the keys, before any
    answer, by an exception the pass does not catch."""
    deliveries: list[PromptDelivery] = field(default_factory=list)
    stops: list[GracefulStopCommand] = field(default_factory=list)
    stop_answer: str = ""
    idle_error: SessionId | None = None
    clock: list[datetime] = field(default_factory=lambda: [NOW])
    out_of_order: list[str] = field(default_factory=list)
    idle_raises: bool = False
    reports: list = field(default_factory=list)
    locks: SessionLocks | None = None
    _pass: RolloverPass | None = None
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
        if session_id == self.idle_error or self.idle_raises:
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
        record = _record(SessionId.new(), self.launched_state, profile=command.profile_id)
        self.sessions[record.session_id] = record
        return _Launched(record)

    async def send(self, session_id: SessionId, text: str) -> PromptDelivery:
        self.sends.append((session_id, text))
        persisted = self._persisted(successor_session_id=session_id)
        self.send_states.append(None if persisted is None else persisted.state)
        if persisted is None:
            self.out_of_order.append("send")
        if self.send_crashes:
            self.send_crashes = False
            raise asyncio.CancelledError
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

    async def told(self, report) -> None:
        self.reports.append(report)

    async def sleep(self, seconds: float) -> None:
        self.clock[0] += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    def later(self, delta: timedelta) -> None:
        self.clock[0] += delta

    def pass_(self) -> RolloverPass:
        """One pass for the rig's life, as `serve` keeps one; restarts are the recovery
        tests' subject."""
        if self._pass is None:
            self._pass = self._new_pass()
        return self._pass

    def _new_pass(self) -> RolloverPass:
        return RolloverPass(
            self.store,
            self.envelopes,
            adopt_templates={CLAUDE: TEMPLATES[CLAUDE]},
            sessions=self.list_sessions,
            handoff_root=lambda project: ROOT if project == PROJECT else None,
            enabled=self.enabled,
            idle=self.idle,
            launch=self.launch,
            send=self.send,
            graceful_stop=self.graceful_stop,
            notify=self.told,
            now=lambda: self.clock[0],
            sleep=self.sleep,
            **({} if self.locks is None else {"operation": self.locks.operation}),
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
    # Every test: each action was licensed by a state already on disk when it was taken, and
    # every stop went only onto an idle composer -- never interrupting a turn (DEC-115).
    assert rig.out_of_order == []
    assert all(command.only_if_idle for command in rig.stops)


async def test_one_ready_across_five_passes_launches_exactly_once(rig: Rig) -> None:
    rig.ready()

    await rig.run(passes=5)

    (command,) = rig.launches
    assert command.idempotency_key == f"rollover:{HANDOFF}"
    assert (command.project_id, command.profile_id) == (PROJECT, CLAUDE)
    # Unattended: a Codex Remote Control change in flight is waited out, not met with the
    # owner's two-second refusal that would fail the rollover (BL-037).
    assert command.guard_wait == UNATTENDED_LAUNCH_WAIT_SECONDS
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

    assert rig.stops == [GracefulStopCommand(rig.predecessor, CLAUDE, only_if_idle=True)]
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


async def test_rollover_types_a_codex_successor_its_own_dollar_adopt_template(
    tmp_path: Path,
) -> None:
    """Each successor is typed its own provider's template: Codex runs the skill as
    `$planning:...`, and a `/` command typed into it would be refused (DEC-122)."""
    rig = Rig(SQLiteRolloverStore(open_database(tmp_path / "sessions.sqlite3")))
    rig.sessions[rig.predecessor] = _record(rig.predecessor, profile=CODEX)
    rig._pass = _templated_pass(rig, TEMPLATES)
    rig.ready()

    await rig.run()

    (command,) = rig.launches
    assert command.profile_id == CODEX
    assert rig.sends == [(rig.successor, f"$planning:executing-plans --adopt-handoff {HANDOFF}")]


async def test_rollover_types_a_claude_successor_the_slash_form_beside_codex(
    tmp_path: Path,
) -> None:
    rig = Rig(SQLiteRolloverStore(open_database(tmp_path / "sessions.sqlite3")))
    rig._pass = _templated_pass(rig, TEMPLATES)
    rig.ready()

    await rig.run()

    assert rig.sends == [(rig.successor, TEMPLATE)]


async def test_rollover_leaves_a_profile_with_no_adopt_template_alone(tmp_path: Path) -> None:
    rig = Rig(SQLiteRolloverStore(open_database(tmp_path / "sessions.sqlite3")))
    rig.sessions[rig.predecessor] = _record(rig.predecessor, profile=ProfileId("opencode"))
    rig._pass = _templated_pass(rig, TEMPLATES)
    rig.ready()

    await rig.run(passes=3)

    assert rig.launches == [] and rig.sends == []


def _templated_pass(rig: Rig, templates) -> RolloverPass:
    return RolloverPass(
        rig.store,
        rig.envelopes,
        adopt_templates=templates,
        sessions=rig.list_sessions,
        handoff_root=lambda project: ROOT if project == PROJECT else None,
        enabled=rig.enabled,
        idle=rig.idle,
        launch=rig.launch,
        send=rig.send,
        graceful_stop=rig.graceful_stop,
        notify=rig.told,
        now=lambda: rig.clock[0],
        sleep=rig.sleep,
    )


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
    # Held on its trust question, and never answered within the patience.
    rig.launched_state = SessionState.UNTRUSTED
    await rig.run()
    rig.later(TRUST_PATIENCE)


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
    assert all(session != rig.predecessor for session, _ in rig.sends), "nothing typed into it"
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


@pytest.mark.parametrize(
    ("deliveries", "detail"),
    [
        # A `/` command whose menu cannot be read: no dialog is up, so it is said as a menu.
        ([PromptDelivery(PromptOutcome.REFUSED, PromptReason.MENU)], "menu"),
        ([PromptDelivery(PromptOutcome.REFUSED, PromptReason.DIALOG)], "dialog"),
        ([PromptDelivery(PromptOutcome.REFUSED, PromptReason.BUSY)] * 100, "not_ready"),
        ([PromptDelivery(PromptOutcome.REFUSED, PromptReason.NOT_RUNNING)] * 100, "not_running"),
    ],
    ids=["menu", "dialog", "busy-throughout", "not-running-throughout"],
)
async def test_why_the_template_was_not_typed_is_worded_as_the_schedules_word_it(
    rig: Rig, deliveries: list[PromptDelivery], detail: str
) -> None:
    rig.deliveries = list(deliveries)
    rig.ready()

    await rig.run()

    rollover = await rig.rollover()
    assert (rollover.failure_code, rollover.failure_detail) == ("not-typed", detail)


async def test_a_stop_refused_under_its_own_key_lock_is_stop_failed_and_types_nothing(
    rig: Rig,
) -> None:
    """The look said idle, and a turn started before the keys: the stop refuses, no key goes."""
    rig.stop_answer = NOT_IDLE
    rig.ready()
    await rig.run()
    rig.accepted_by(rig.successor)

    await rig.run(passes=2)

    rollover = await rig.rollover()
    assert (rollover.state, rollover.failure_code) == (
        RolloverState.STOP_FAILED,
        "predecessor-not-idle",
    )
    assert len(rig.stops) == 1 and rig.stops[0].only_if_idle
    assert rig.sessions[rig.predecessor].state is SessionState.RUNNING


async def test_a_replayed_ready_of_a_finished_handoff_keeps_the_owners_live_request(
    rig: Rig,
) -> None:
    rig.ready()
    rig.deliveries = [PromptDelivery(PromptOutcome.REFUSED, PromptReason.DIALOG)]
    await rig.run()
    assert (await rig.rollover()).state is RolloverState.FAILED
    asked = await rig.store.request(rig.predecessor, project_id=PROJECT, profile_id=CLAUDE, at=NOW)
    assert asked is not None
    await rig.run()
    assert rig.envelopes.request == str(rig.predecessor)

    rig.ready()  # the finished handoff's `ready`, written again
    await rig.run(passes=2)

    assert rig.envelopes.request == str(rig.predecessor)
    assert (await rig.store.get(asked.id)).state is RolloverState.REQUESTED


async def test_one_request_per_checkout_is_out_at_a_time(rig: Rig) -> None:
    other = SessionId.new()
    rig.sessions[other] = _record(other)
    first = await rig.store.request(rig.predecessor, project_id=PROJECT, profile_id=CLAUDE, at=NOW)
    second = await rig.store.request(
        other, project_id=PROJECT, profile_id=CLAUDE, at=NOW + timedelta(seconds=1)
    )
    assert first is not None and second is not None

    await rig.run(passes=3)
    assert rig.envelopes.requests == [str(rig.predecessor)], "the later request waits"
    # And says so on its own row, once, naming whose request holds the checkout.
    waiting = [e.detail for e in await rig.store.events(second.id) if e.detail]
    assert waiting.count(f"waiting: {rig.predecessor}'s request holds this checkout") == 1

    await rig.store.advance(first.id, RolloverState.CANCELLED, at=NOW)
    await rig.run()

    assert rig.envelopes.requests == [str(rig.predecessor), str(other)]
    assert rig.envelopes.request == str(other)


async def test_a_request_that_could_not_be_written_fails_rather_than_waiting(rig: Rig) -> None:
    rig.envelopes.refuse_writes = True
    await rig.store.request(rig.predecessor, project_id=PROJECT, profile_id=CLAUDE, at=NOW)

    await rig.run(passes=2)

    rollover = await rig.rollover()
    assert (rollover.state, rollover.failure_code) == (RolloverState.FAILED, "request-unwritten")


async def test_a_request_found_gone_is_given_up_never_written_again(rig: Rig) -> None:
    await rig.store.request(rig.predecessor, project_id=PROJECT, profile_id=CLAUDE, at=NOW)
    await rig.run()
    rig.envelopes.request = None  # a crash between record and write, or someone removed it

    await rig.run(passes=2)

    rollover = await rig.rollover()
    assert (rollover.state, rollover.failure_code) == (RolloverState.FAILED, "request-lost")
    assert rig.envelopes.requests == [str(rig.predecessor)]


async def test_a_request_for_a_session_that_cannot_roll_over_fails_at_once(rig: Rig) -> None:
    rig.sessions[rig.predecessor] = _record(rig.predecessor, profile=ProfileId("codex"))
    await rig.store.request(
        rig.predecessor, project_id=PROJECT, profile_id=ProfileId("codex"), at=NOW
    )

    await rig.run()

    rollover = await rig.rollover()
    assert (rollover.state, rollover.failure_code) == (RolloverState.FAILED, "not-rollable")
    assert rig.envelopes.requests == []


async def test_a_look_that_raises_reads_as_not_idle_and_its_patience_runs(rig: Rig) -> None:
    rig.ready()
    rig.idle_raises = True

    await rig.run()
    assert (await rig.rollover()).state is RolloverState.HANDOFF_READY
    rig.later(NOT_IDLE_PATIENCE)
    await rig.run()

    rollover = await rig.rollover()
    assert (rollover.state, rollover.failure_code) == (RolloverState.FAILED, "predecessor-not-idle")
    assert rig.launches == []


async def test_unreadable_envelopes_do_not_hold_back_an_accepted_rollovers_stop(rig: Rig) -> None:
    rig.ready()
    await rig.run()
    successor = rig.successor
    (row,) = await rig.store.open_rollovers()
    await rig.store.advance(row.id, RolloverState.SUCCESSOR_ACCEPTED, at=NOW)
    rig.envelopes.events_error = True

    await rig.run()

    assert len(rig.stops) == 1
    assert (await rig.rollover()).state is RolloverState.COMPLETED
    assert rig.sessions[successor].state is SessionState.RUNNING


async def test_a_failure_is_told_even_when_its_clean_up_raises(rig: Rig) -> None:
    """The move is made once, so a notice skipped behind a failed clean-up is never made."""
    rig.deliveries = [PromptDelivery(PromptOutcome.REFUSED, PromptReason.DIALOG)]
    rig.envelopes.discard_error = True
    rig.ready()

    await rig.run(passes=2)

    assert (await rig.rollover()).state is RolloverState.FAILED
    assert [report.rollover.failure_code for report in rig.reports] == ["not-typed"]


async def test_a_ready_nobody_was_watching_for_is_never_acted_on(rig: Rig) -> None:
    """Written while the switch was off (the plugin then tells the owner to resume by hand) and
    found only once it was turned on: acting on it would put a second session on a plan the
    owner may already have resumed. It is cleared, and nothing is opened or launched."""
    rig.envelopes.files.append(
        HandoffEnvelope(
            HandoffEvent.READY,
            HANDOFF,
            str(rig.predecessor),
            NOW - READY_FRESHNESS - timedelta(seconds=1),
            "/abs/plan.md",
            None,
        )
    )

    await rig.run(passes=2)

    assert rig.store._select("1 = 1", ()) == ()
    assert rig.launches == [] and rig.stops == []
    assert HANDOFF in rig.envelopes.discarded


async def test_a_ready_written_moments_ago_is_acted_on(rig: Rig) -> None:
    rig.envelopes.files.append(
        HandoffEnvelope(
            HandoffEvent.READY,
            HANDOFF,
            str(rig.predecessor),
            NOW - READY_FRESHNESS + timedelta(seconds=1),
            "/abs/plan.md",
            None,
        )
    )

    await rig.run()

    assert len(rig.launches) == 1


async def test_a_request_unanswered_for_24_hours_fails_and_frees_the_checkout(rig: Rig) -> None:
    """A plan that never reaches another gate never answers; the slot is not held for ever."""
    other = SessionId.new()
    rig.sessions[other] = _record(other)
    first = await rig.store.request(rig.predecessor, project_id=PROJECT, profile_id=CLAUDE, at=NOW)
    second = await rig.store.request(
        other, project_id=PROJECT, profile_id=CLAUDE, at=NOW + timedelta(hours=1)
    )
    assert first is not None and second is not None
    await rig.run()
    assert rig.envelopes.request == str(rig.predecessor)

    rig.later(timedelta(hours=24, seconds=1))
    await rig.run()

    ended = next(r for r in rig.store._select("1 = 1", ()) if r.id == first.id)
    assert (ended.state, ended.failure_code) == (RolloverState.FAILED, "request-unanswered")
    assert rig.envelopes.requests == [str(rig.predecessor), str(other)]
    assert rig.envelopes.request == str(other), "the second request in the checkout proceeds"
    assert [(r.rollover.id, r.rollover.failure_code) for r in rig.reports] == [
        (first.id, "request-unanswered")
    ], "exactly one FAILED notice, naming the code"
    assert rig.launches == [] and rig.stops == []


async def test_a_request_23_hours_59_minutes_old_keeps_waiting(rig: Rig) -> None:
    await rig.store.request(rig.predecessor, project_id=PROJECT, profile_id=CLAUDE, at=NOW)
    await rig.run()

    rig.later(timedelta(hours=23, minutes=59))
    await rig.run()

    rollover = await rig.rollover()
    assert rollover.state is RolloverState.REQUESTED
    assert rig.envelopes.request == str(rig.predecessor)
    assert rig.reports == []


async def test_a_rollover_met_by_the_shutdown_drain_launches_nothing_and_stays_ready(
    rig: Rig,
) -> None:
    """BL-130: a pass waking during `serve`'s drain used to fail the rollover for good, as
    LAUNCH_FAILED. The gate now refuses before SUCCESSOR_STARTING is persisted."""
    rig.locks = SessionLocks()
    await rig.locks.drain()
    rig.ready()

    await rig.run()

    assert rig.launches == []
    assert (await rig.rollover()).state is RolloverState.HANDOFF_READY


async def test_a_rollover_launching_when_the_drain_begins_finishes_its_launch(rig: Rig) -> None:
    """The hold spans persist-and-launch, and the session service's own gate inside the launch
    is admitted as part of it, so the drain waits for the launch rather than refusing it."""
    locks = rig.locks = SessionLocks()
    plain = rig.launch
    draining: list[asyncio.Task] = []

    async def launch_as_the_service_does(command):
        draining.append(asyncio.create_task(locks.drain()))
        await asyncio.sleep(0)
        async with locks.operation():
            return await plain(command)

    rig.launch = launch_as_the_service_does  # type: ignore[method-assign]
    rig.ready()

    await rig.run()

    (drain,) = draining
    await asyncio.wait_for(drain, timeout=1)
    assert len(rig.launches) == 1
    assert (await rig.rollover()).successor_session_id is not None
    assert rig.out_of_order == []


async def test_a_predecessor_stop_met_by_the_shutdown_drain_is_not_spent(rig: Rig) -> None:
    rig.locks = SessionLocks()
    rig.ready()
    await rig.run()
    assert (await rig.rollover()).state is RolloverState.ADOPTING, "the premise"
    rig.accepted_by(rig.successor)
    await rig.locks.drain()

    await rig.run()

    assert rig.stops == []
    assert (await rig.rollover()).state is RolloverState.SUCCESSOR_ACCEPTED


# -- per-session roots (BL-116) ------------------------------------------------------------

WORKTREE_ROOT = Path("/state/worktrees/remote-agents/wt")
OTHER_HANDOFF = "h-fedcba9876543210fedc"


@dataclass
class RootedEnvelopes(Envelopes):
    """Envelopes per checkout: a worktree's top level is a checkout of its own."""

    by_root: dict[Path, list[HandoffEnvelope]] = field(default_factory=dict)
    read_roots: list[Path] = field(default_factory=list)
    requests_at: dict[Path, str] = field(default_factory=dict)

    def events(self, project_dir: Path) -> tuple[HandoffEnvelope, ...]:
        self.read_roots.append(project_dir)
        return tuple(self.by_root.get(project_dir, ()))

    def write_request(self, project_dir: Path, managed_session_id: str) -> bool:
        self.requests_at[project_dir] = managed_session_id
        return True

    def requested(self, project_dir: Path) -> str | None:
        return self.requests_at.get(project_dir)

    def discard(self, project_dir: Path, handoff_id: str) -> None:
        self.discarded.append(handoff_id)
        self.by_root[project_dir] = [
            e for e in self.by_root.get(project_dir, []) if e.handoff_id != handoff_id
        ]


def _rooted_pass(rig: Rig, worktree_roots) -> RolloverPass:
    return RolloverPass(
        rig.store,
        rig.envelopes,
        adopt_templates={CLAUDE: TEMPLATES[CLAUDE]},
        sessions=rig.list_sessions,
        handoff_root=lambda project: ROOT if project == PROJECT else None,
        worktree_roots=worktree_roots,
        enabled=rig.enabled,
        idle=rig.idle,
        launch=rig.launch,
        send=rig.send,
        graceful_stop=rig.graceful_stop,
        now=lambda: rig.clock[0],
        sleep=rig.sleep,
    )


@pytest.fixture
def rooted(tmp_path: Path) -> Rig:
    return Rig(
        SQLiteRolloverStore(open_database(tmp_path / "sessions.sqlite3")),
        envelopes=RootedEnvelopes(),
    )


async def test_a_worktree_session_and_a_main_session_each_roll_over_from_their_own_root(
    rooted: Rig,
) -> None:
    """One project, two checkouts: each `ready` is read under its own top level, picked up
    for the session that wrote it, and each successor reuses its own predecessor's folder."""
    in_worktree = SessionId.new()
    rooted.sessions[in_worktree] = _record(in_worktree)
    envelopes = rooted.envelopes
    assert isinstance(envelopes, RootedEnvelopes)
    envelopes.by_root[ROOT] = [_envelope(HandoffEvent.READY, rooted.predecessor)]
    envelopes.by_root[WORKTREE_ROOT] = [
        HandoffEnvelope(HandoffEvent.READY, OTHER_HANDOFF, str(in_worktree), NOW, "/p.md", None)
    ]

    async def worktree_roots():
        return {in_worktree: WORKTREE_ROOT}

    await _rooted_pass(rooted, worktree_roots).run_once()

    rows = {row.predecessor_session_id: row for row in rooted.store._select("1 = 1", ())}
    assert set(rows) == {rooted.predecessor, in_worktree}
    assert rows[in_worktree].handoff_id == OTHER_HANDOFF
    assert rows[rooted.predecessor].handoff_id == HANDOFF
    assert {command.reuse_folder_of for command in rooted.launches} == {
        rooted.predecessor,
        in_worktree,
    }


async def test_a_session_whose_worktree_is_gone_is_never_rolled_over_from_the_main_checkout(
    rooted: Rig,
) -> None:
    """Its root is None, not the project's: a `ready` in the main checkout naming it is
    someone else's file, and acting on it would start its successor in the wrong folder."""
    envelopes = rooted.envelopes
    assert isinstance(envelopes, RootedEnvelopes)
    envelopes.by_root[ROOT] = [_envelope(HandoffEvent.READY, rooted.predecessor)]

    async def worktree_roots():
        return {rooted.predecessor: None}

    await _rooted_pass(rooted, worktree_roots).run_once()

    assert rooted.store._select("1 = 1", ()) == ()
    assert rooted.launches == []


async def test_a_successor_is_launched_to_reuse_its_predecessors_folder(rig: Rig) -> None:
    rig.ready()

    await rig.run()

    (command,) = rig.launches
    assert command.reuse_folder_of == rig.predecessor


# -- a successor held on its folder-trust dialog ---------------------------------------------


def _trust(rig: Rig) -> None:
    """The owner answers the successor's trust question: it comes up on its composer."""
    rig.sessions[rig.successor] = _record(rig.successor)


async def test_a_trust_held_successor_is_typed_once_only_after_it_is_trusted(rig: Rig) -> None:
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED

    await rig.run(passes=3)

    rollover = await rig.rollover()
    assert rollover.state is RolloverState.SUCCESSOR_STARTING
    assert rollover.successor_session_id == rig.successor
    assert TRUST_HOLD in [e.detail for e in await rig.store.events(rollover.id)]
    assert len(rig.launches) == 1
    assert rig.sends == [] and rig.stops == [] and rig.reports == []

    _trust(rig)
    await rig.run(passes=3)

    assert rig.sends == [(rig.successor, TEMPLATE)]
    # Persisted before the keys, so a restart part-way can never type it again.
    assert rig.send_states == [RolloverState.ADOPTING]
    assert (await rig.rollover()).state is RolloverState.ADOPTING
    assert rig.stops == []

    rig.accepted_by(rig.successor)
    await rig.run()

    assert (await rig.rollover()).state is RolloverState.COMPLETED
    assert rig.stops == [GracefulStopCommand(rig.predecessor, CLAUDE, only_if_idle=True)]
    assert len(rig.sends) == 1


async def test_a_held_successor_whose_profile_lost_its_template_is_never_typed(rig: Rig) -> None:
    """A restart after the vertical stopped declaring a template (a Codex revert, Stage 2's
    rollback) types nothing it did not declare: the open rollover fails as not typed."""
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED
    await rig.run()
    assert (await rig.rollover()).state is RolloverState.SUCCESSOR_STARTING

    rig._pass = _templated_pass(rig, {})
    _trust(rig)
    await rig.run(passes=3)

    assert rig.sends == [] and rig.stops == []
    rollover = await rig.rollover()
    assert rollover.state is RolloverState.FAILED
    assert (rollover.failure_code, rollover.failure_detail) == ("not-typed", "no-adopt-template")


async def test_a_trust_held_successor_is_given_up_only_after_the_patience(rig: Rig) -> None:
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED
    await rig.run()

    rig.later(TRUST_PATIENCE - timedelta(seconds=1))
    await rig.run()
    assert (await rig.rollover()).state is RolloverState.SUCCESSOR_STARTING

    rig.later(timedelta(seconds=1))
    await rig.run(passes=2)

    rollover = await rig.rollover()
    assert (rollover.state, rollover.failure_code) == (RolloverState.FAILED, "successor-untrusted")
    assert [report.rollover.failure_code for report in rig.reports] == ["successor-untrusted"]
    assert rig.sends == [] and rig.stops == []


async def test_a_successor_declined_on_its_trust_dialog_fails_the_rollover(rig: Rig) -> None:
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED
    await rig.run()

    # "No, exit": the agent quits, and its session ends.
    rig.sessions[rig.successor] = _record(rig.successor, SessionState.ENDED)
    await rig.run()

    rollover = await rig.rollover()
    assert (rollover.state, rollover.failure_code) == (RolloverState.FAILED, "successor-failed")
    assert rig.sends == [] and rig.stops == []


async def test_a_restart_across_the_trust_hold_types_once_after_trust(rig: Rig) -> None:
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED
    await rig.run()

    rig._pass = None  # a restart while the question is still up
    await rig.run()
    rollover = await rig.rollover()
    assert rollover.state is RolloverState.SUCCESSOR_STARTING
    assert "restart: wait" in [e.detail for e in await rig.store.events(rollover.id)]
    assert rig.sends == []

    _trust(rig)
    rig._pass = None  # and another, after the owner trusted it
    await rig.run(passes=2)

    assert rig.sends == [(rig.successor, TEMPLATE)]
    assert "restart: type_once" in [e.detail for e in await rig.store.events(rollover.id)]
    assert (await rig.rollover()).state is RolloverState.ADOPTING


async def test_a_pass_killed_while_typing_after_trust_never_types_again(rig: Rig) -> None:
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED
    await rig.run()
    _trust(rig)
    rig.send_crashes = True

    with pytest.raises(asyncio.CancelledError):
        await rig.run()
    assert len(rig.sends) == 1

    rig._pass = None  # the process that was typing is gone
    await rig.run(passes=3)

    assert len(rig.sends) == 1
    assert (await rig.rollover()).state is RolloverState.ADOPTING
    assert rig.stops == []


async def test_a_refused_send_after_trust_fails_not_typed_and_is_never_retried(rig: Rig) -> None:
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED
    await rig.run()
    _trust(rig)
    rig.deliveries = [PromptDelivery(PromptOutcome.REFUSED, PromptReason.DIALOG)]

    await rig.run(passes=3)

    rollover = await rig.rollover()
    assert (rollover.state, rollover.failure_code) == (RolloverState.FAILED, "not-typed")
    assert len(rig.sends) == 1
    assert rig.send_states == [RolloverState.ADOPTING]
    assert rig.stops == []


async def test_a_row_moved_under_the_trusted_pass_is_left_as_it_is_and_untyped(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED
    await rig.run()
    _trust(rig)
    events = rig.store.events

    async def moved_meanwhile(rollover_id: str) -> tuple[RolloverEvent, ...]:
        # Another writer ends the row after this pass read it open, before its ADOPTING move.
        read = tuple(await events(rollover_id))
        monkeypatch.setattr(rig.store, "events", events)
        await rig.store.advance(rollover_id, RolloverState.FAILED, at=NOW, failure_code="other")
        return read

    monkeypatch.setattr(rig.store, "events", moved_meanwhile)
    await rig.run()

    rollover = await rig.rollover()
    assert (rollover.state, rollover.failure_code) == (RolloverState.FAILED, "other")
    assert rig.sends == [] and rig.stops == []


async def test_a_pass_ended_just_after_recording_types_once_after_a_trust_given_meanwhile(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED
    record_successor = rig.store.record_successor

    async def recorded_then_gone(*args: object, **kwargs: object) -> Rollover:
        # The successor's row commits, and the process dies before anything after it runs.
        await record_successor(*args, **kwargs)  # type: ignore[arg-type]
        raise asyncio.CancelledError

    monkeypatch.setattr(rig.store, "record_successor", recorded_then_gone)
    with pytest.raises(asyncio.CancelledError):
        await rig.run()
    monkeypatch.setattr(rig.store, "record_successor", record_successor)
    rollover = await rig.rollover()
    assert rollover.successor_session_id == rig.successor
    # The hold went in with the record, in one transaction, so nothing could separate them.
    assert TRUST_HOLD in [e.detail for e in await rig.store.events(rollover.id)]

    _trust(rig)  # while the service was down
    rig._pass = None
    await rig.run(passes=2)

    assert rig.sends == [(rig.successor, TEMPLATE)]
    assert "restart: type_once" in [e.detail for e in await rig.store.events(rollover.id)]
    assert (await rig.rollover()).state is RolloverState.ADOPTING


async def test_a_row_recorded_without_its_hold_is_held_on_the_next_pass(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A row a 0.63.0 pass left: the successor recorded, the hold note never written.
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED
    record_successor = rig.store.record_successor

    async def unheld(
        rollover_id: str, successor: SessionId, *, at: datetime, held: bool = False
    ) -> Rollover:
        return await record_successor(rollover_id, successor, at=at)

    monkeypatch.setattr(rig.store, "record_successor", unheld)
    await rig.run()
    rollover = await rig.rollover()
    assert rollover.successor_session_id == rig.successor
    assert TRUST_HOLD not in [e.detail for e in await rig.store.events(rollover.id)]

    await rig.run(passes=2)
    rollover = await rig.rollover()
    assert rollover.state is RolloverState.SUCCESSOR_STARTING
    assert TRUST_HOLD in [e.detail for e in await rig.store.events(rollover.id)]
    assert rig.sends == []

    _trust(rig)
    await rig.run()

    assert rig.sends == [(rig.successor, TEMPLATE)]
    assert (await rig.rollover()).state is RolloverState.ADOPTING


async def test_a_trusted_successor_waits_for_its_predecessor_to_be_idle_again(rig: Rig) -> None:
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED
    await rig.run()
    # The owner went back to the old session during the hold: its pane is working.
    rig.not_idle.add(rig.predecessor)
    _trust(rig)

    await rig.run(passes=3)

    assert rig.sends == []
    assert (await rig.rollover()).state is RolloverState.SUCCESSOR_STARTING

    rig.not_idle.discard(rig.predecessor)
    await rig.run()

    assert rig.sends == [(rig.successor, TEMPLATE)]
    assert (await rig.rollover()).state is RolloverState.ADOPTING


async def test_a_predecessor_still_working_long_after_the_trust_fails_the_rollover_untyped(
    rig: Rig,
) -> None:
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED
    await rig.run()
    # Trusted near the end of the trust patience, with the old session working at that moment.
    rig.later(TRUST_PATIENCE - timedelta(minutes=1))
    rig.not_idle.add(rig.predecessor)
    _trust(rig)

    await rig.run(passes=3)
    rollover = await rig.rollover()
    assert rollover.state is RolloverState.SUCCESSOR_STARTING
    # Written once, however many passes find it waiting: its time is the wait's start.
    details = [e.detail for e in await rig.store.events(rollover.id)]
    assert details.count(TRUSTED_WAITING) == 1

    # Past the trust patience, the wait for idle has its own clock, started at the trust.
    rig.later(NOT_IDLE_PATIENCE - timedelta(seconds=1))
    await rig.run()
    assert (await rig.rollover()).state is RolloverState.SUCCESSOR_STARTING

    rig.later(timedelta(seconds=1))
    await rig.run()

    rollover = await rig.rollover()
    assert (rollover.state, rollover.failure_code) == (RolloverState.FAILED, "predecessor-not-idle")
    assert rig.sends == [] and rig.stops == []


async def test_a_predecessor_idle_again_within_its_wait_is_typed_once(rig: Rig) -> None:
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED
    await rig.run()
    rig.later(TRUST_PATIENCE - timedelta(minutes=1))
    rig.not_idle.add(rig.predecessor)
    _trust(rig)
    await rig.run()

    rig.later(timedelta(minutes=5))  # past the trust patience, within the wait's own
    rig.not_idle.discard(rig.predecessor)
    await rig.run(passes=2)

    assert rig.sends == [(rig.successor, TEMPLATE)]
    assert (await rig.rollover()).state is RolloverState.ADOPTING


async def test_a_predecessor_gone_during_the_hold_fails_the_rollover_untyped(rig: Rig) -> None:
    rig.ready()
    rig.launched_state = SessionState.UNTRUSTED
    await rig.run()
    rig.sessions[rig.predecessor] = _record(rig.predecessor, SessionState.ENDED)
    _trust(rig)

    await rig.run()

    rollover = await rig.rollover()
    assert (rollover.state, rollover.failure_code) == (RolloverState.FAILED, "predecessor-gone")
    assert rig.sends == [] and rig.stops == []
