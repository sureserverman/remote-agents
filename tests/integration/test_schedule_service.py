"""The fire pass inside the bot's service: fire, type, tell -- over the real stores.

Task 2.4 of the scheduled-sessions plan: `composition/service.py` ticks `SchedulePass` every 30 s
where the bot's composition wired one, a pass that raises costs one tick, and the local surface
fires nothing.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.limit_stop_store import SQLiteLimitStopStore
from remote_agents.adapters.sqlite.schedule_store import SQLiteScheduleStore
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.telegram.flood import FloodGate
from remote_agents.adapters.telegram.schedule_notifications import ScheduleNotifier
from remote_agents.application.schedules import SchedulePass, limit_stopped_in, still_working_in
from remote_agents.composition import service
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.agent_activity import ActivityKind, AgentActivity, LimitHit
from remote_agents.ports.schedules import Repeat, Schedule
from remote_agents.ports.terminal import PromptDelivery, PromptOutcome

FIRE = datetime(2026, 10, 2, 7, 0, tzinfo=UTC)
SRC = Path(__file__).resolve().parents[2] / "src" / "remote_agents"


@pytest.fixture
def connection(tmp_path):
    connection = open_database(tmp_path / "sessions.sqlite3")
    yield connection
    connection.close()


def _schedule(schedule_id: str = "s1", *, last: str | None = None) -> Schedule:
    return Schedule(
        id=schedule_id,
        project_id=ProjectId("p-opaque"),
        profile_id=ProfileId("claude"),
        prompt="reply with OK",
        when=Repeat.daily(time(9, 0)),
        paused=False,
        next_fire_at=FIRE,
        created_at=FIRE - timedelta(days=1),
        last_session_id=last,
    )


def _record(session_id: SessionId, state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("p-opaque"),
        ProfileId("claude"),
        SessionDisplayIdentity("p-opaque", "claude", "regular", 1),
        state,
        FIRE - timedelta(hours=1),
    )


class _View:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_apart(self, bot, arguments) -> int:
        self.sent.append(str(arguments["text"]))
        return len(self.sent)


class _Markers:
    def __init__(self, *running: str) -> None:
        self.running = set(running)

    def started_at(self, session_id: str):
        return FIRE if session_id in self.running else None


def _pass(connection, *, view: _View, launches: list, markers: _Markers | None = None):
    sessions = SQLiteSessionStore(connection)

    async def launch(command):
        launches.append(command)
        if not await sessions.claim_idempotency_key(command.idempotency_key):
            from remote_agents.application.errors import DuplicateCommandError

            raise DuplicateCommandError("already handled")
        record = _record(SessionId.new())
        await sessions.save(record)
        return SimpleNamespace(record=record, remote_control=False)

    async def send(session_id, text):
        return PromptDelivery(PromptOutcome.SENT)

    notifier = ScheduleNotifier(
        view=view, project_name=lambda _: "remote-agents", flood=FloodGate()
    )
    notifier.attach(object())
    return SchedulePass(
        SQLiteScheduleStore(connection),
        launch=launch,
        send=send,
        limit_stopped=limit_stopped_in(sessions, SQLiteLimitStopStore(connection)),
        working=still_working_in(sessions, markers or _Markers()),
        notify=notifier.notify,
        zone=lambda: ZoneInfo("Europe/Berlin"),  # 09:00 Berlin is FIRE, 07:00Z
        now=lambda: FIRE + timedelta(seconds=10),
    ), notifier


async def _ticks(composition, seconds: float) -> None:
    loop = asyncio.create_task(service._fire_schedules_periodically(composition, 0.01))
    await asyncio.sleep(seconds)
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)


async def test_a_due_schedule_fires_types_and_tells_once_over_three_ticks(connection) -> None:
    await SQLiteScheduleStore(connection).add(_schedule())
    view, launches = _View(), []
    schedule_pass, notifier = _pass(connection, view=view, launches=launches)
    composition = SimpleNamespace(schedule_pass=schedule_pass, schedule_notifier=notifier)

    await _ticks(composition, 0.1)

    assert len(launches) == 1
    assert launches[0].idempotency_key == f"sched:s1:{FIRE.isoformat()}"
    assert view.sent == ["Scheduled: claude in remote-agents started"]
    advanced = await SQLiteScheduleStore(connection).get("s1")
    assert advanced is not None and advanced.next_fire_at == FIRE + timedelta(days=1)


async def test_a_restart_mid_fire_launches_nothing_again(connection) -> None:
    """The key is claimed in the domain store, so a second process finds it taken (rule 8)."""
    await SQLiteScheduleStore(connection).add(_schedule())
    await SQLiteSessionStore(connection).claim_idempotency_key(f"sched:s1:{FIRE.isoformat()}")
    view, launches = _View(), []
    schedule_pass, _ = _pass(connection, view=view, launches=launches)

    (report,) = await schedule_pass.run()

    assert report.outcome.value == "duplicate"
    assert view.sent == [
        "Scheduled: claude in remote-agents may have started before a restart, and its message "
        "was not typed — check its sessions"
    ]


async def test_a_running_session_with_an_undecided_limit_stop_skips_its_agent(connection) -> None:
    stopped = SessionId.new()
    await SQLiteSessionStore(connection).save(_record(stopped))
    await SQLiteActivityStore(connection).append(
        AgentActivity(
            str(stopped),
            ActivityKind.LIMIT_REACHED,
            None,
            FIRE - timedelta(minutes=5),
            limit=LimitHit("5h", FIRE + timedelta(hours=1)),
        )
    )
    await SQLiteScheduleStore(connection).add(_schedule())
    view, launches = _View(), []
    schedule_pass, _ = _pass(connection, view=view, launches=launches)

    (report,) = await schedule_pass.run()

    assert report.outcome.value == "skipped_limit"
    assert launches == []
    assert view.sent == [
        "Scheduled: claude in remote-agents skipped — claude is at its usage limit"
    ]


async def test_a_previous_run_is_working_only_while_live_with_a_turn_marker(connection) -> None:
    """Liveness and a marker, not the marker's age — the six-hour window is the next test.

    `_Markers.started_at` returns the fixed `FIRE`. `still_working_in`'s default clock is the
    wall, so once `FIRE` is more than six hours ago a live session reads as idle and this
    assertion becomes a date. It passed on 2026-10-02, four hours after `FIRE`, and failed
    on every runner after that.
    """
    sessions = SQLiteSessionStore(connection)
    live, ended = SessionId.new(), SessionId.new()
    await sessions.save(_record(live))
    await sessions.save(_record(ended, SessionState.ENDED))

    def during_the_turn() -> datetime:
        return FIRE + timedelta(minutes=1)

    working = still_working_in(sessions, _Markers(str(live), str(ended)), now=during_the_turn)
    idle = still_working_in(sessions, _Markers(), now=during_the_turn)

    assert await working(str(live)) is True
    assert await working(str(ended)) is False
    assert await working(str(SessionId.new())) is False
    assert await idle(str(live)) is False


async def test_a_turn_marker_left_by_an_esc_holds_runs_back_for_six_hours_at_most(
    connection,
) -> None:
    sessions = SQLiteSessionStore(connection)
    live = SessionId.new()
    await sessions.save(_record(live))
    markers = _Markers(str(live))  # started at FIRE

    young = still_working_in(sessions, markers, now=lambda: FIRE + timedelta(hours=5, minutes=59))
    stale = still_working_in(sessions, markers, now=lambda: FIRE + timedelta(hours=6))

    assert await young(str(live)) is True
    assert await stale(str(live)) is False


async def test_a_pass_that_raises_does_not_end_the_loop() -> None:
    calls = 0

    class _Raising:
        async def run(self):
            nonlocal calls
            calls += 1
            raise RuntimeError("the store is locked")

    await _ticks(SimpleNamespace(schedule_pass=_Raising(), schedule_notifier=None), 0.1)

    assert calls >= 2


async def test_a_held_notice_is_retried_on_the_next_tick() -> None:
    retried = 0

    class _Notifier:
        async def pass_once(self) -> None:
            nonlocal retried
            retried += 1

    class _Quiet:
        async def run(self):
            return ()

    await _ticks(SimpleNamespace(schedule_pass=_Quiet(), schedule_notifier=_Notifier()), 0.1)

    assert retried >= 2


def test_the_poll_is_thirty_seconds() -> None:
    assert service._SCHEDULE_POLL_SECONDS == 30.0


def test_a_composition_without_a_fire_pass_has_none_by_default() -> None:
    assert service.ServiceComposition.__dataclass_fields__["schedule_pass"].default is None


def test_the_local_surface_composes_no_fire_pass() -> None:
    """Firing is the bot's service alone: the TUI composition names neither the pass nor the
    loop, so two processes can never both fire one schedule."""
    tree = ast.parse((SRC / "composition" / "tui.py").read_text(encoding="utf-8"))
    names = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Name | ast.Attribute)
    }
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert not ({"SchedulePass", "_fire_schedules_periodically"} & (names | imported))


def test_the_bot_composition_wires_the_pass_to_its_notifier(tmp_path, monkeypatch) -> None:
    from test_composition_store_wiring import _composition

    composition, paths, connection, ui = _composition(tmp_path, monkeypatch)
    try:
        assert isinstance(composition.schedule_pass, SchedulePass)
        assert composition.schedule_notifier is composition.boundary.schedule_notifier
        assert composition.schedule_notifier is not None
    finally:
        connection.close()
        ui.close()
