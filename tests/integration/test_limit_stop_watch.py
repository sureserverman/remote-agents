"""A lifted limit stop is noticed once and its line retired, and a restart does not do it again.

Limit-lifecycle sub-plan 2 Task 2.2: the real domain database (migrations applied, stops appended
through the real activity store, outcomes through the real outcome store) under the application's
lift pass. The session store and the retire call are doubles; `retire_line` itself is covered in
`tests/unit/adapters/telegram/test_notifications.py`.
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
from remote_agents.domain.models import SessionState
from remote_agents.ports.agent_activity import ActivityKind, AgentActivity, LimitHit
from remote_agents.ports.agent_usage import AgentLimits, UsageWindow
from remote_agents.ports.limit_stop_outcomes import LIFTED

_A = "11111111-1111-4111-8111-111111111111"
_B = "22222222-2222-4222-8222-222222222222"
_STOP = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
_RESET = _STOP + timedelta(hours=2)
_AFTER = _RESET + timedelta(minutes=2)


class _Sessions:
    """The session store's `list`, honouring the state filter the way the real one does."""

    def __init__(self, **states: SessionState) -> None:
        self.states = {_A: states.get("a", SessionState.RUNNING), _B: states.get("b")}

    async def list(self, states=None):
        return [
            SimpleNamespace(session_id=session, profile_id="codex", state=state)
            for session, state in self.states.items()
            if state is not None and (states is None or state in states)
        ]


class _Retire:
    def __init__(self, *, fail: bool = False, done: bool = True) -> None:
        self.calls: list[str] = []
        self.fail = fail
        self.done = done

    async def __call__(self, stop) -> bool:
        self.calls.append(stop.session_id)
        if self.fail:
            raise RuntimeError("the chat is unreachable")
        return self.done


def _stop(
    session: str = _A, at: datetime = _STOP, window: str = "5h", resets_at: datetime = _RESET
) -> AgentActivity:
    return AgentActivity(
        session, ActivityKind.LIMIT_REACHED, None, at, limit=LimitHit(window, resets_at)
    )


async def _no_readings() -> tuple[AgentLimits, ...]:
    return ()


def _watcher(connection, sessions, retire, *, now=_AFTER, limits=_no_readings):
    return LimitLiftWatcher(
        sessions, SQLiteLimitStopStore(connection), limits, retire, now=lambda: now
    )


@pytest.fixture
def connection(tmp_path):
    connection = open_database(tmp_path / "state.sqlite3", migrations=MIGRATIONS)
    yield connection
    connection.close()


async def test_lifted_stop_is_retired_once(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    retire = _Retire()
    watcher = _watcher(connection, _Sessions(), retire)

    assert await watcher.pass_once() == 1
    assert await watcher.pass_once() == 0
    assert retire.calls == [_A]
    (row,) = connection.execute("SELECT session_id, outcome FROM limit_stop_outcomes").fetchall()
    assert row == (_A, LIFTED)


async def test_lifted_stop_is_not_retired_again_after_a_restart(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    first = open_database(path, migrations=MIGRATIONS)
    await SQLiteActivityStore(first).append(_stop())
    retire = _Retire()
    assert await _watcher(first, _Sessions(), retire).pass_once() == 1
    first.close()

    second = open_database(path, migrations=MIGRATIONS)
    assert await _watcher(second, _Sessions(), retire).pass_once() == 0
    second.close()
    assert retire.calls == [_A]


async def test_lifted_not_yet_records_nothing(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    retire = _Retire()

    assert await _watcher(connection, _Sessions(), retire, now=_STOP).pass_once() == 0
    assert retire.calls == []
    assert connection.execute("SELECT COUNT(*) FROM limit_stop_outcomes").fetchone() == (0,)


async def test_lifted_early_on_a_reading_after_the_stop(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    retire = _Retire()

    async def limits():
        return (
            AgentLimits(
                "codex",
                (UsageWindow("5h", 3, _RESET + timedelta(hours=5)),),
                observed_at=_STOP + timedelta(minutes=20),
            ),
        )

    now = _STOP + timedelta(minutes=21)
    assert await _watcher(connection, _Sessions(), retire, now=now, limits=limits).pass_once() == 1


@pytest.mark.parametrize("state", [SessionState.STOP_REQUESTED, SessionState.ENDED])
async def test_lifted_is_never_asked_of_a_session_that_is_not_running(connection, state) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    retire = _Retire()

    assert await _watcher(connection, _Sessions(a=state), retire).pass_once() == 0
    assert retire.calls == []


async def test_lifted_is_only_asked_of_a_session_whose_newest_news_is_the_stop(connection) -> None:
    """Later news already retired the bot's line (Stage 1), and there is nothing to resume."""
    store = SQLiteActivityStore(connection)
    await store.append(_stop())
    await store.append(
        AgentActivity(_A, ActivityKind.COMPLETED, "done", _STOP + timedelta(minutes=5))
    )
    await store.append(_stop(_B))
    retire = _Retire()

    assert await _watcher(connection, _Sessions(b=SessionState.RUNNING), retire).pass_once() == 1
    assert retire.calls == [_B]


async def test_lifted_but_unretired_is_tried_again_next_pass(connection) -> None:
    await SQLiteActivityStore(connection).append(_stop())
    failing = _Retire(fail=True)

    assert await _watcher(connection, _Sessions(), failing).pass_once() == 0
    assert connection.execute("SELECT COUNT(*) FROM limit_stop_outcomes").fetchone() == (0,)

    working = _Retire()
    assert await _watcher(connection, _Sessions(), working).pass_once() == 1
    assert working.calls == [_A]


async def test_lifted_a_second_stop_after_a_resolved_one_is_its_own_stop(connection) -> None:
    store = SQLiteActivityStore(connection)
    await store.append(_stop())
    retire = _Retire()
    assert await _watcher(connection, _Sessions(), retire).pass_once() == 1

    later = _AFTER + timedelta(hours=1)
    await store.append(
        _stop(at=later - timedelta(minutes=30), resets_at=later + timedelta(hours=1))
    )
    assert await _watcher(connection, _Sessions(), retire, now=later).pass_once() == 0, (
        "its own reset has not passed"
    )
    assert retire.calls == [_A]
    assert (
        await _watcher(
            connection, _Sessions(), retire, now=later + timedelta(hours=1, minutes=2)
        ).pass_once()
        == 1
    )
    assert retire.calls == [_A, _A]


async def test_lifted_pass_survives_a_failing_limits_read(connection) -> None:
    """The schedule needs no reading; a reader that raises costs the early witness only."""
    await SQLiteActivityStore(connection).append(_stop())
    retire = _Retire()

    async def broken():
        raise RuntimeError("reader down")

    assert await _watcher(connection, _Sessions(), retire, limits=broken).pass_once() == 1


async def test_lifted_but_not_yet_retirable_records_nothing(connection) -> None:
    """The surface said the stop is not done -- its notification is still waiting to be sent."""
    await SQLiteActivityStore(connection).append(_stop())
    waiting = _Retire(done=False)

    assert await _watcher(connection, _Sessions(), waiting).pass_once() == 0
    assert connection.execute("SELECT COUNT(*) FROM limit_stop_outcomes").fetchone() == (0,)
    assert await _watcher(connection, _Sessions(), _Retire()).pass_once() == 1


async def test_lifted_outcome_joins_the_stamp_exactly_as_it_was_stored(connection) -> None:
    """Keyed on the stored text, not a re-serialised instant, so no row format can miss the join."""
    connection.execute(
        "INSERT INTO agent_activity(session_id, kind, detail, confidence, observed_at,"
        " limit_window, limit_resets_at) VALUES (?, 'limit_reached', NULL, 'reported', ?, '5h', ?)",
        (_A, "2026-09-29T10:00:00", _RESET.isoformat()),
    )
    connection.commit()
    retire = _Retire()

    assert await _watcher(connection, _Sessions(), retire).pass_once() == 1
    assert await _watcher(connection, _Sessions(), retire).pass_once() == 0
    assert retire.calls == [_A]


async def test_lifted_watch_loop_survives_a_pass_that_hangs(monkeypatch) -> None:
    """A wedged limits read or Telegram call costs one bounded pass, never every later lift."""
    import asyncio

    from remote_agents.composition import service

    monkeypatch.setattr(service, "_LIMIT_STOP_PASS_TIMEOUT_SECONDS", 0.05)
    calls = 0

    class _Watcher:
        async def pass_once(self) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.Event().wait()
            return 0

    composition = SimpleNamespace(limit_lift_watcher=_Watcher())
    loop = asyncio.create_task(service._watch_limit_stops_periodically(composition, 0.01))
    await asyncio.sleep(0.3)
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)

    assert calls >= 2
