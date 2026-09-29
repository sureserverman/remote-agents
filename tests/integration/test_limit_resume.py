"""A lifted limit stop is nudged once, through the terminal's guards, and never twice.

Limit-lifecycle sub-plan 2 Task 3.3: the real domain database (stops appended through the real
activity store, outcomes through the real outcome store) under the application's lift pass with
its resume step. The terminal's send, the switch, and the bot's line calls are doubles; the
guards the send keeps are the terminal's own (`tests/live/test_prompt_relay.py`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.limit_stop_store import SQLiteLimitStopStore
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.application.limit_lifts import LimitLiftWatcher
from remote_agents.application.limit_resume import (
    BUSY_PATIENCE,
    NUDGE,
    RESUMED,
    LimitResume,
    NotResumed,
    not_resumed,
)
from remote_agents.domain.models import SessionState
from remote_agents.ports.agent_activity import ActivityKind, AgentActivity, LimitHit
from remote_agents.ports.agent_usage import AgentLimits, UsageWindow
from remote_agents.ports.limit_stop_outcomes import LIFTED
from remote_agents.ports.session_store import SessionEvent
from remote_agents.ports.terminal import PromptDelivery, PromptOutcome, PromptReason

_A = "11111111-1111-4111-8111-111111111111"
_STOP = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
_RESET = _STOP + timedelta(hours=2)
_AFTER = _RESET + timedelta(minutes=2)

SENT = PromptDelivery(PromptOutcome.SENT)
BUSY = PromptDelivery(PromptOutcome.REFUSED, PromptReason.BUSY)
DIALOG = PromptDelivery(PromptOutcome.REFUSED, PromptReason.DIALOG)
COMPOSING = PromptDelivery(PromptOutcome.REFUSED, PromptReason.COMPOSING)
UNCONFIRMED = PromptDelivery(PromptOutcome.UNCONFIRMED, PromptReason.SUBMIT_NOT_SEEN)


class _Clock:
    def __init__(self, moment: datetime = _AFTER) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class _Sessions:
    """`list` and `events`, the two things the lift pass asks of the session store."""

    def __init__(self, profile: str = "codex", events: tuple[SessionEvent, ...] = ()) -> None:
        self.profile = profile
        self.history = events

    async def list(self, states=None):
        record = SimpleNamespace(session_id=_A, profile_id=self.profile, state=SessionState.RUNNING)
        return [record] if states is None or SessionState.RUNNING in states else []

    async def events(self, _session_id):
        return self.history


class _Terminal:
    """The terminal's guarded send, scripted: each call answers the next delivery."""

    def __init__(self, *deliveries: PromptDelivery) -> None:
        self.deliveries = list(deliveries)
        self.sent: list[tuple[str, str]] = []

    async def send(self, session_id, text: str) -> PromptDelivery:
        self.sent.append((str(session_id), text))
        return self.deliveries.pop(0) if self.deliveries else SENT


class _Line:
    """The bot's two line calls and its readiness answer."""

    def __init__(self, *, settled: bool = True) -> None:
        self.retired: list[str] = []
        self.amended: list[tuple[str, str]] = []
        self.ready = settled

    async def retire(self, stop) -> bool:
        self.retired.append(stop.session_id)
        return True

    async def amend(self, stop, reason: str) -> bool:
        self.amended.append((stop.session_id, reason))
        return True

    async def settled(self, _stop) -> bool:
        return self.ready


async def _no_readings() -> tuple[AgentLimits, ...]:
    return ()


def _stop(at: datetime = _STOP, resets_at: datetime = _RESET) -> AgentActivity:
    return AgentActivity(_A, ActivityKind.LIMIT_REACHED, None, at, limit=LimitHit("5h", resets_at))


def _watcher(connection, terminal, line, clock, *, sessions=None, on=True, limits=_no_readings):
    async def enabled() -> bool:
        return on

    resume = LimitResume(
        send=terminal.send, enabled=enabled, settled=line.settled, amend=line.amend, now=clock
    )
    return LimitLiftWatcher(
        sessions if sessions is not None else _Sessions(),
        SQLiteLimitStopStore(connection),
        limits,
        line.retire,
        resume=resume,
        now=clock,
    )


def _outcomes(connection) -> list[str]:
    return [row[0] for row in connection.execute("SELECT outcome FROM limit_stop_outcomes")]


@pytest.fixture
def connection(tmp_path):
    connection = open_database(tmp_path / "state.sqlite3", migrations=MIGRATIONS)
    yield connection
    connection.close()


async def test_a_lifted_stop_is_nudged_once_and_its_line_retired(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    terminal, line = _Terminal(SENT), _Line()
    watcher = _watcher(connection, terminal, line, _Clock())

    assert await watcher.pass_once() == 1
    assert await watcher.pass_once() == 0

    assert terminal.sent == [(_A, NUDGE)]
    assert line.retired == [_A] and line.amended == []
    assert _outcomes(connection) == [RESUMED]


async def test_busy_then_idle_is_nudged_on_the_later_pass(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    clock = _Clock()
    terminal, line = _Terminal(BUSY, SENT), _Line()
    watcher = _watcher(connection, terminal, line, clock)

    assert await watcher.pass_once() == 0
    assert _outcomes(connection) == [] and line.retired == []
    clock.moment += timedelta(minutes=5)
    assert await watcher.pass_once() == 1

    assert len(terminal.sent) == 2
    assert _outcomes(connection) == [RESUMED]


async def test_busy_past_the_patience_is_not_resumed_and_the_line_says_so(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    clock = _Clock()
    terminal, line = _Terminal(BUSY, BUSY, BUSY), _Line()
    watcher = _watcher(connection, terminal, line, clock)

    assert await watcher.pass_once() == 0
    clock.moment += BUSY_PATIENCE - timedelta(seconds=1)
    assert await watcher.pass_once() == 0
    clock.moment += timedelta(seconds=2)
    assert await watcher.pass_once() == 1
    assert await watcher.pass_once() == 0

    assert len(terminal.sent) == 3
    assert _outcomes(connection) == [not_resumed(NotResumed.BUSY)]
    assert line.amended == [(_A, "busy")] and line.retired == []


@pytest.mark.parametrize(
    ("delivery", "reason"),
    [
        (DIALOG, NotResumed.DIALOG),
        (COMPOSING, NotResumed.COMPOSING),
        (PromptDelivery(PromptOutcome.REFUSED, PromptReason.UNRECOGNISED), NotResumed.UNRECOGNISED),
        (UNCONFIRMED, NotResumed.UNCONFIRMED),
    ],
)
async def test_a_refusal_that_waiting_cannot_fix_is_recorded_at_once(
    connection, delivery, reason
) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    terminal, line = _Terminal(delivery), _Line()
    watcher = _watcher(connection, terminal, line, _Clock())

    assert await watcher.pass_once() == 1
    assert await watcher.pass_once() == 0

    assert len(terminal.sent) == 1, "never tried again"
    assert _outcomes(connection) == [not_resumed(reason)]
    assert line.amended == [(_A, reason.value)]


async def test_the_switch_off_retires_the_line_and_types_nothing(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    terminal, line = _Terminal(), _Line()
    watcher = _watcher(connection, terminal, line, _Clock(), on=False)

    assert await watcher.pass_once() == 1

    assert terminal.sent == []
    assert line.retired == [_A]
    assert _outcomes(connection) == [LIFTED]


async def test_a_restart_after_resumed_sends_nothing(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    first = open_database(path, migrations=MIGRATIONS)
    await SQLiteActivityStore(first).append(_stop())
    terminal, line = _Terminal(SENT), _Line()
    assert await _watcher(first, terminal, line, _Clock()).pass_once() == 1
    first.close()

    second = open_database(path, migrations=MIGRATIONS)
    assert await _watcher(second, terminal, line, _Clock()).pass_once() == 0
    second.close()

    assert terminal.sent == [(_A, NUDGE)]


async def test_a_cursor_stop_with_no_finished_event_is_not_nudged_again(connection) -> None:
    """Cursor reports nothing after a nudge, so its newest news stays the stop -- the recorded
    outcome is the only thing keeping it from being typed into every pass."""
    await SQLiteActivityStore(connection).append(_stop())
    terminal, line = _Terminal(SENT, SENT, SENT), _Line()
    watcher = _watcher(connection, terminal, line, _Clock(), sessions=_Sessions("cursor"))

    for _ in range(3):
        await watcher.pass_once()

    assert terminal.sent == [(_A, NUDGE)]


async def test_a_session_that_changed_life_after_the_stop_is_not_nudged(connection) -> None:
    """A lifecycle event after the stop -- a stop requested and timed out, a trust prompt
    answered -- means the stop is not this life's; its line goes, and nothing is typed."""
    await SQLiteActivityStore(connection).append(_stop())
    later = SessionEvent("graceful_stop_timed_out", _STOP + timedelta(minutes=30), None)
    terminal, line = _Terminal(), _Line()
    sessions = _Sessions(events=(SessionEvent("ready", _STOP - timedelta(hours=1), None), later))
    watcher = _watcher(connection, terminal, line, _Clock(), sessions=sessions)

    assert await watcher.pass_once() == 1

    assert terminal.sent == []
    assert line.retired == [_A]
    assert _outcomes(connection) == [LIFTED]


async def test_a_stop_whose_line_is_not_settled_waits_and_types_nothing(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    terminal, line = _Terminal(), _Line(settled=False)
    watcher = _watcher(connection, terminal, line, _Clock())

    assert await watcher.pass_once() == 0

    assert terminal.sent == [] and _outcomes(connection) == []


async def test_a_stop_right_after_a_nudge_waits_for_its_own_schedule(connection) -> None:
    """A nudge that ran straight into the limit again must not loop: the new stop is lifted
    early by no reading, only by its own published reset."""
    activities = SQLiteActivityStore(connection)
    await activities.append(_stop())
    clock = _Clock()
    terminal, line = _Terminal(SENT, SENT), _Line()
    reading_time: list[datetime] = []

    async def limits():
        if not reading_time:
            return ()
        return (
            AgentLimits(
                "codex",
                (UsageWindow("5h", 3, second_reset + timedelta(hours=5)),),
                observed_at=reading_time[0],
            ),
        )

    watcher = _watcher(connection, terminal, line, clock, limits=limits)
    assert await watcher.pass_once() == 1
    nudged_at = clock.moment

    second_reset = nudged_at + timedelta(hours=5)
    await activities.append(_stop(at=nudged_at + timedelta(minutes=1), resets_at=second_reset))
    clock.moment = nudged_at + timedelta(minutes=20)
    reading_time.append(clock.moment - timedelta(minutes=1))
    assert await watcher.pass_once() == 0, "an early witness does not lift a looped stop"

    clock.moment = second_reset + timedelta(minutes=2)
    reading_time[0] = clock.moment - timedelta(minutes=1)
    assert await watcher.pass_once() == 1
    assert len(terminal.sent) == 2


class _FailingOutcomes(SQLiteLimitStopStore):
    """The real store, whose first `record` raises -- the database refusing one write."""

    def __init__(self, connection) -> None:
        super().__init__(connection)
        self.failures = 1

    async def record(self, stop, outcome, *, decided_at) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("database is locked")
        await super().record(stop, outcome, decided_at=decided_at)


async def test_a_record_that_fails_after_the_send_never_types_twice(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    clock = _Clock()
    terminal, line = _Terminal(SENT, SENT), _Line()

    async def enabled() -> bool:
        return True

    resume = LimitResume(
        send=terminal.send, enabled=enabled, settled=line.settled, amend=line.amend, now=clock
    )
    watcher = LimitLiftWatcher(
        _Sessions(),
        _FailingOutcomes(connection),
        _no_readings,
        line.retire,
        resume=resume,
        now=clock,
    )

    await watcher.pass_once()
    await watcher.pass_once()

    assert terminal.sent == [(_A, NUDGE)]
    assert _outcomes(connection) == [RESUMED], "the second pass recorded what the first decided"


async def test_a_pass_cancelled_mid_send_still_records_the_nudge(connection) -> None:
    """The service bounds each pass; a bound that fires while the nudge is typing must not
    leave it unrecorded, or the next pass types it again."""
    import asyncio

    await SQLiteActivityStore(connection).append(_stop())
    started = asyncio.Event()
    release = asyncio.Event()
    sent: list[str] = []

    async def slow_send(session_id, text):
        # Typed first, then waiting to see it land -- the window a bound can fire in.
        sent.append(text)
        started.set()
        await release.wait()
        return SENT

    line = _Line()
    clock = _Clock()

    async def enabled() -> bool:
        return True

    resume = LimitResume(
        send=slow_send, enabled=enabled, settled=line.settled, amend=line.amend, now=clock
    )
    watcher = LimitLiftWatcher(
        _Sessions(),
        SQLiteLimitStopStore(connection),
        _no_readings,
        line.retire,
        resume=resume,
        now=clock,
    )
    task = asyncio.create_task(watcher.pass_once())
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(20):
        await asyncio.sleep(0)
        if _outcomes(connection):
            break

    await watcher.pass_once()

    assert sent == [NUDGE]
    assert _outcomes(connection) == [RESUMED]


@pytest.mark.parametrize("failing", ["retire", "amend"])
async def test_a_line_call_that_raises_after_the_record_does_not_nudge_again(
    connection, failing
) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    terminal = _Terminal(SENT if failing == "retire" else DIALOG, SENT)
    line = _Line()

    async def boom(*_args) -> bool:
        raise RuntimeError("the chat is unreachable")

    setattr(line, failing, boom)
    watcher = _watcher(connection, terminal, line, _Clock())

    assert await watcher.pass_once() == 1
    assert await watcher.pass_once() == 0

    assert len(terminal.sent) == 1
    assert len(_outcomes(connection)) == 1


async def test_a_send_that_raises_is_given_up_as_unconfirmed(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    line = _Line()
    calls: list[str] = []

    async def raising(_session_id, text):
        calls.append(text)
        raise RuntimeError("tmux vanished")

    async def enabled() -> bool:
        return True

    clock = _Clock()
    resume = LimitResume(
        send=raising, enabled=enabled, settled=line.settled, amend=line.amend, now=clock
    )
    watcher = LimitLiftWatcher(
        _Sessions(),
        SQLiteLimitStopStore(connection),
        _no_readings,
        line.retire,
        resume=resume,
        now=clock,
    )

    assert await watcher.pass_once() == 1
    assert await watcher.pass_once() == 0

    assert calls == [NUDGE]
    assert _outcomes(connection) == [not_resumed(NotResumed.UNCONFIRMED)]


async def test_a_session_gone_by_the_send_is_retired_as_lifted(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    terminal = _Terminal(PromptDelivery(PromptOutcome.REFUSED, PromptReason.NOT_RUNNING))
    line = _Line()

    assert await _watcher(connection, terminal, line, _Clock()).pass_once() == 1

    assert _outcomes(connection) == [LIFTED]
    assert line.retired == [_A] and line.amended == []


async def test_another_sender_at_the_keys_waits_like_busy(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    terminal = _Terminal(PromptDelivery(PromptOutcome.REFUSED, PromptReason.KEYS_BUSY), SENT)
    line = _Line()
    watcher = _watcher(connection, terminal, line, _Clock())

    assert await watcher.pass_once() == 0
    assert await watcher.pass_once() == 1
    assert _outcomes(connection) == [RESUMED]


async def test_a_command_menu_is_given_up_as_a_dialog(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    terminal = _Terminal(PromptDelivery(PromptOutcome.REFUSED, PromptReason.MENU))

    assert await _watcher(connection, terminal, _Line(), _Clock()).pass_once() == 1
    assert _outcomes(connection) == [not_resumed(NotResumed.DIALOG)]


async def test_a_line_that_settles_later_is_nudged_exactly_once(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    terminal, line = _Terminal(SENT, SENT), _Line(settled=False)
    watcher = _watcher(connection, terminal, line, _Clock())

    assert await watcher.pass_once() == 0
    line.ready = True
    assert await watcher.pass_once() == 1
    assert await watcher.pass_once() == 0
    assert terminal.sent == [(_A, NUDGE)]


# Stage 3 gate, remediation round 1 -----------------------------------------------------------


async def test_a_nudge_interrupted_before_its_record_is_never_typed_again(connection) -> None:
    """A process that died between the send and the record left its intent row. The next
    process cannot know whether the text landed, so it gives the stop up as unconfirmed."""
    await SQLiteActivityStore(connection).append(_stop())
    outcomes = SQLiteLimitStopStore(connection)
    (stop,) = await outcomes.unresolved([_A])
    await outcomes.claim(stop, decided_at=_AFTER)
    terminal, line = _Terminal(SENT), _Line()

    assert await _watcher(connection, terminal, line, _Clock()).pass_once() == 1

    assert terminal.sent == []
    assert _outcomes(connection) == [not_resumed(NotResumed.UNCONFIRMED)]
    assert line.amended == [(_A, "unconfirmed")]


async def test_a_busy_pass_leaves_no_intent_behind(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    terminal, line = _Terminal(BUSY), _Line()

    assert await _watcher(connection, terminal, line, _Clock()).pass_once() == 0

    assert _outcomes(connection) == []


async def test_a_line_that_does_not_land_after_the_record_is_retried_without_typing(
    connection,
) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    terminal = _Terminal(SENT, SENT)
    line = _Line()
    answers = [False, False, True]

    async def retire(stop) -> bool:
        line.retired.append(stop.session_id)
        return answers.pop(0)

    line.retire = retire  # type: ignore[method-assign]
    watcher = _watcher(connection, terminal, line, _Clock())

    for _ in range(4):
        await watcher.pass_once()

    assert terminal.sent == [(_A, NUDGE)]
    assert line.retired == [_A, _A, _A], "retried until it landed, then left alone"


async def test_a_pass_cancelled_mid_send_still_updates_the_line(connection) -> None:
    import asyncio

    await SQLiteActivityStore(connection).append(_stop())
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_send(session_id, text):
        started.set()
        await release.wait()
        return SENT

    line, clock = _Line(), _Clock()

    async def enabled() -> bool:
        return True

    resume = LimitResume(
        send=slow_send, enabled=enabled, settled=line.settled, amend=line.amend, now=clock
    )
    watcher = LimitLiftWatcher(
        _Sessions(), SQLiteLimitStopStore(connection), _no_readings, line.retire,
        resume=resume, now=clock,
    )  # fmt: skip
    task = asyncio.create_task(watcher.pass_once())
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(50):
        await asyncio.sleep(0)

    assert _outcomes(connection) == [RESUMED]
    assert line.retired == [_A]


async def test_a_looped_stop_with_an_already_passed_reset_is_not_nudged(connection) -> None:
    """The loop guard's schedule exemption holds only for a reset still ahead at the nudge; a
    re-stop carrying a reset that had already passed is exactly a false lift repeating."""
    activities = SQLiteActivityStore(connection)
    await activities.append(_stop())
    clock = _Clock()
    terminal, line = _Terminal(SENT, SENT), _Line()
    watcher = _watcher(connection, terminal, line, clock)
    assert await watcher.pass_once() == 1

    await activities.append(_stop(at=clock.moment + timedelta(minutes=1), resets_at=_RESET))
    clock.moment += timedelta(minutes=3)
    assert await watcher.pass_once() == 0

    assert len(terminal.sent) == 1


async def test_the_switch_is_read_again_just_before_the_send(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    terminal, line = _Terminal(SENT), _Line()
    answers = [True, False]

    async def enabled() -> bool:
        return answers.pop(0) if answers else False

    clock = _Clock()
    resume = LimitResume(
        send=terminal.send, enabled=enabled, settled=line.settled, amend=line.amend, now=clock
    )
    watcher = LimitLiftWatcher(
        _Sessions(), SQLiteLimitStopStore(connection), _no_readings, line.retire,
        resume=resume, now=clock,
    )  # fmt: skip

    assert await watcher.pass_once() == 0

    assert terminal.sent == []
    assert _outcomes(connection) == []
