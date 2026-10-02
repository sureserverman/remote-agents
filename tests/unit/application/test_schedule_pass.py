"""The fire pass: rules 4, 5, 6 and 8, then the first prompt's delivery (rules 2 and 7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from schedule_fakes import MemoryScheduleStore

from remote_agents.application.commands import LaunchCommand
from remote_agents.application.errors import DuplicateCommandError
from remote_agents.application.schedules import (
    MISSED_GRACE,
    FireOutcome,
    FireReport,
    SchedulePass,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.schedules import Once, Repeat, Schedule
from remote_agents.ports.terminal import PromptDelivery, PromptOutcome

BERLIN = ZoneInfo("Europe/Berlin")
FIRE = datetime(2026, 10, 2, 7, 0, tzinfo=UTC)  # 09:00 Berlin, a Friday
SENT = PromptDelivery(PromptOutcome.SENT)


def _schedule(schedule_id: str = "s1", *, when=None, fire: datetime = FIRE, last=None) -> Schedule:
    return Schedule(
        id=schedule_id,
        project_id=ProjectId("remote-agents"),
        profile_id=ProfileId("claude"),
        prompt="reply with OK",
        when=when if when is not None else Repeat.daily(time(9, 0)),
        paused=False,
        next_fire_at=fire,
        created_at=FIRE - timedelta(days=3),
        last_session_id=last,
    )


@dataclass
class Rig:
    """Fakes for everything the pass is handed, recording what it asked of each."""

    launches: list[LaunchCommand] = field(default_factory=list)
    sends: list[tuple[SessionId, str]] = field(default_factory=list)
    reports: list[FireReport] = field(default_factory=list)
    deliveries: list[PromptDelivery] = field(default_factory=list)
    stopped: set[str] = field(default_factory=set)
    working: set[str] = field(default_factory=set)
    launch_error: Exception | None = None
    launched_state: SessionState = SessionState.RUNNING
    clock: list[datetime] = field(default_factory=list)

    async def launch(self, command: LaunchCommand):
        self.launches.append(command)
        if self.launch_error is not None:
            raise self.launch_error
        record = SessionRecord(
            SessionId.new(),
            command.project_id,
            command.profile_id,
            SessionDisplayIdentity("remote-agents", "claude", "regular", 1, command.label),
            self.launched_state,
            FIRE,
        )
        return _Launched(record)

    async def send(self, session_id: SessionId, text: str) -> PromptDelivery:
        self.sends.append((session_id, text))
        return self.deliveries.pop(0) if self.deliveries else SENT

    async def limit_stopped(self, profile_id: ProfileId) -> bool:
        return str(profile_id) in self.stopped

    async def is_working(self, session_id: str) -> bool:
        return session_id in self.working

    async def notify(self, report: FireReport) -> None:
        self.reports.append(report)

    async def sleep(self, seconds: float) -> None:
        self.clock[0] += timedelta(seconds=seconds)

    def pass_for(self, store: MemoryScheduleStore, now: datetime) -> SchedulePass:
        self.clock[:] = [now]
        return SchedulePass(
            store,
            launch=self.launch,
            send=self.send,
            limit_stopped=self.limit_stopped,
            working=self.is_working,
            notify=self.notify,
            zone=lambda: BERLIN,
            now=lambda: self.clock[0],
            sleep=self.sleep,
        )


@dataclass(frozen=True)
class _Launched:
    record: SessionRecord
    remote_control: bool = False


async def _run(store: MemoryScheduleStore, rig: Rig, now: datetime) -> tuple[FireReport, ...]:
    return await rig.pass_for(store, now).run()


async def test_a_due_schedule_launches_once_with_its_fire_key() -> None:
    store, rig = MemoryScheduleStore(_schedule()), Rig()

    reports = await _run(store, rig, FIRE + timedelta(seconds=20))

    assert [report.outcome for report in reports] == [FireOutcome.STARTED]
    (command,) = rig.launches
    assert command.idempotency_key == f"sched:s1:{FIRE.isoformat()}"
    assert command.project_id == ProjectId("remote-agents")
    assert command.profile_id == ProfileId("claude")
    assert rig.reports == list(reports)


async def test_a_schedule_not_yet_due_is_left_alone() -> None:
    store, rig = MemoryScheduleStore(_schedule()), Rig()

    assert await _run(store, rig, FIRE - timedelta(seconds=1)) == ()
    assert rig.launches == [] and rig.reports == []


async def test_fourteen_minutes_late_still_fires() -> None:
    store, rig = MemoryScheduleStore(_schedule()), Rig()

    (report,) = await _run(store, rig, FIRE + timedelta(minutes=14))

    assert report.outcome is FireOutcome.STARTED
    assert len(rig.launches) == 1


async def test_sixteen_minutes_late_is_missed_and_says_how_late() -> None:
    store, rig = MemoryScheduleStore(_schedule()), Rig()

    (report,) = await _run(store, rig, FIRE + timedelta(minutes=16))

    assert report.outcome is FireOutcome.MISSED
    assert report.late_by == timedelta(minutes=16)
    assert rig.launches == []
    assert MISSED_GRACE == timedelta(minutes=15)


async def test_an_agent_with_an_active_limit_stop_is_skipped() -> None:
    store, rig = MemoryScheduleStore(_schedule()), Rig(stopped={"claude"})

    (report,) = await _run(store, rig, FIRE)

    assert report.outcome is FireOutcome.SKIPPED_LIMIT
    assert rig.launches == []


async def test_a_previous_run_still_working_is_skipped() -> None:
    store, rig = MemoryScheduleStore(_schedule(last="prev")), Rig(working={"prev"})

    (report,) = await _run(store, rig, FIRE)

    assert report.outcome is FireOutcome.SKIPPED_PREVIOUS
    assert rig.launches == []


async def test_a_previous_run_that_is_idle_does_not_block_the_next() -> None:
    store, rig = MemoryScheduleStore(_schedule(last="prev")), Rig()

    (report,) = await _run(store, rig, FIRE)

    assert report.outcome is FireOutcome.STARTED


async def test_a_duplicate_key_launches_nothing_and_is_recorded() -> None:
    store = MemoryScheduleStore(_schedule())
    rig = Rig(launch_error=DuplicateCommandError("launch callback was already handled"))

    (report,) = await _run(store, rig, FIRE)

    assert report.outcome is FireOutcome.DUPLICATE
    assert rig.sends == []
    advanced = await store.get("s1")
    assert advanced is not None and advanced.next_fire_at > FIRE


async def test_a_launch_that_raises_is_reported_with_its_error() -> None:
    store, rig = MemoryScheduleStore(_schedule()), Rig(launch_error=RuntimeError("tmux gone"))

    (report,) = await _run(store, rig, FIRE)

    assert report.outcome is FireOutcome.LAUNCH_FAILED
    assert rig.sends == []


async def test_a_launch_recorded_failed_is_reported_and_types_nothing() -> None:
    store, rig = MemoryScheduleStore(_schedule()), Rig(launched_state=SessionState.FAILED)

    (report,) = await _run(store, rig, FIRE)

    assert report.outcome is FireOutcome.LAUNCH_FAILED
    assert rig.sends == []


def _branches() -> dict[str, tuple[Rig, timedelta, str | None]]:
    return {
        "started": (Rig(), timedelta(0), None),
        "missed": (Rig(), timedelta(minutes=16), None),
        "limit": (Rig(stopped={"claude"}), timedelta(0), None),
        "previous": (Rig(working={"prev"}), timedelta(0), "prev"),
        "duplicate": (Rig(launch_error=DuplicateCommandError("x")), timedelta(0), None),
        "failed": (Rig(launch_error=RuntimeError("x")), timedelta(0), None),
    }


@pytest.mark.parametrize("branch", list(_branches()))
async def test_a_recurring_schedule_advances_to_its_next_time_on_every_branch(
    branch: str,
) -> None:
    rig, late, last = _branches()[branch]
    store = MemoryScheduleStore(_schedule(last=last))

    await _run(store, rig, FIRE + late)

    advanced = await store.get("s1")
    assert advanced is not None
    # Tomorrow 09:00 Berlin, never a backlog of the run that was missed or skipped.
    assert advanced.next_fire_at == FIRE + timedelta(days=1)
    assert len(rig.reports) == 1


@pytest.mark.parametrize("branch", list(_branches()))
async def test_a_one_shot_is_gone_after_any_branch(branch: str) -> None:
    rig, late, last = _branches()[branch]
    store = MemoryScheduleStore(_schedule(when=Once(datetime(2026, 10, 2, 9, 0)), last=last))

    await _run(store, rig, FIRE + late)

    assert await store.get("s1") is None
    assert len(rig.reports) == 1


async def test_a_started_fire_records_its_session() -> None:
    store, rig = MemoryScheduleStore(_schedule()), Rig()

    (report,) = await _run(store, rig, FIRE)

    advanced = await store.get("s1")
    assert advanced is not None
    assert report.session_id is not None
    assert advanced.last_session_id == report.session_id
    assert advanced.last_fire_at == FIRE


async def test_a_second_pass_over_the_same_tick_fires_nothing_more() -> None:
    store, rig = MemoryScheduleStore(_schedule()), Rig()

    await _run(store, rig, FIRE)
    await _run(store, rig, FIRE + timedelta(seconds=30))

    assert len(rig.launches) == 1
    assert len(rig.reports) == 1


async def test_a_report_names_the_schedule_it_is_about() -> None:
    store, rig = MemoryScheduleStore(_schedule()), Rig()

    (report,) = await _run(store, rig, FIRE)

    assert report.schedule.id == "s1"
    assert report.schedule.profile_id == ProfileId("claude")
