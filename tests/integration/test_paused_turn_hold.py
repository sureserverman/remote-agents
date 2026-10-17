"""A Claude turn that paused for its own background work says "finished" only after a quiet spell.

DEC-129, with the owner's choice of 2026-10-17: the hook marks a `Stop` whose `background_tasks`
still lists work in flight, and the service holds that "finished" back. The session waking again
-- any later activity, or a turn its hook marked after the pause -- drops the hold; two quiet
minutes release it as an ordinary `COMPLETED`, so a dev server left running cannot silence the
session for good.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.composition.service import (
    _PAUSED_QUIET_SECONDS,
    ServiceComposition,
    _hold_paused_turns,
    _watch_activity_once,
)
from remote_agents.ports.agent_activity import ActivityKind, AgentActivity

_SESSION = "0b6f2c1e-5d7a-4c3b-9e8f-1a2b3c4d5e6f"
_OTHER = "1c7a3d2f-6e8b-4d4c-8f9a-2b3c4d5e6f70"
_PAUSED_AT = datetime(2026, 10, 17, 19, 11, tzinfo=UTC)


def _activity(
    kind: ActivityKind = ActivityKind.COMPLETED, *, paused: bool = False, session: str = _SESSION
) -> AgentActivity:
    return AgentActivity(session, kind, "The shell is running.", _PAUSED_AT, paused=paused)


class _Markers:
    def __init__(self, started: dict[str, datetime] | None = None) -> None:
        self._started = started or {}

    def started_at(self, session_id: str) -> datetime | None:
        return self._started.get(session_id)


def _composition(markers: _Markers | None = None) -> SimpleNamespace:
    return SimpleNamespace(paused_turns={}, turn_markers=markers)


def test_a_paused_finish_is_held_back() -> None:
    composition = _composition()

    assert _hold_paused_turns(composition, [_activity(paused=True)], now=0.0) == []
    assert _SESSION in composition.paused_turns


def test_it_is_released_as_an_ordinary_finish_after_the_quiet_spell() -> None:
    composition = _composition()
    _hold_paused_turns(composition, [_activity(paused=True)], now=0.0)

    assert _hold_paused_turns(composition, [], now=_PAUSED_QUIET_SECONDS - 1) == []
    released_at = _PAUSED_AT + timedelta(seconds=_PAUSED_QUIET_SECONDS)
    (released,) = _hold_paused_turns(
        composition, [], now=_PAUSED_QUIET_SECONDS, clock=lambda: released_at
    )

    assert released.kind is ActivityKind.COMPLETED and not released.paused
    assert released.detail == "The shell is running."
    assert composition.paused_turns == {}
    # Stamped with its release: the feed orders by storage and ages by stamp, so the pause's
    # stamp put a late row above newer ones with an older age.
    assert released.observed_at == released_at


def test_a_real_finish_replaces_the_hold_and_is_delivered_at_once() -> None:
    composition = _composition()
    _hold_paused_turns(composition, [_activity(paused=True)], now=0.0)

    (finished,) = _hold_paused_turns(composition, [_activity()], now=30.0)

    assert not finished.paused
    assert _hold_paused_turns(composition, [], now=_PAUSED_QUIET_SECONDS * 2) == []


def test_an_ask_after_the_pause_drops_the_hold() -> None:
    composition = _composition()
    _hold_paused_turns(composition, [_activity(paused=True)], now=0.0)

    (ask,) = _hold_paused_turns(composition, [_activity(ActivityKind.NEEDS_ANSWER)], now=5.0)

    assert ask.kind is ActivityKind.NEEDS_ANSWER
    assert _hold_paused_turns(composition, [], now=_PAUSED_QUIET_SECONDS * 2) == []


def test_a_pause_then_a_finish_in_one_pass_delivers_only_the_finish() -> None:
    composition = _composition()

    (finished,) = _hold_paused_turns(composition, [_activity(paused=True), _activity()], now=0.0)

    assert not finished.paused and composition.paused_turns == {}


def test_a_turn_marked_after_the_pause_means_the_session_woke() -> None:
    """The wake-up turn's `UserPromptSubmit` starts a marker, and its own `Stop` will decide."""
    composition = _composition(_Markers({_SESSION: _PAUSED_AT + timedelta(seconds=40)}))
    _hold_paused_turns(composition, [_activity(paused=True)], now=0.0)

    assert _hold_paused_turns(composition, [], now=_PAUSED_QUIET_SECONDS) == []
    assert composition.paused_turns == {}


def test_a_marker_from_before_the_pause_does_not_count_as_waking() -> None:
    composition = _composition(_Markers({_SESSION: _PAUSED_AT - timedelta(seconds=40)}))
    _hold_paused_turns(composition, [_activity(paused=True)], now=0.0)

    (released,) = _hold_paused_turns(composition, [], now=_PAUSED_QUIET_SECONDS)

    assert released.kind is ActivityKind.COMPLETED


def test_one_sessions_hold_leaves_the_others_activity_alone() -> None:
    composition = _composition()

    passing = _hold_paused_turns(
        composition, [_activity(paused=True), _activity(session=_OTHER)], now=0.0
    )

    assert [activity.session_id for activity in passing] == [_OTHER]
    assert list(composition.paused_turns) == [_SESSION]


class _Notifier:
    def __init__(self) -> None:
        self.delivered: list[AgentActivity] = []

    async def deliver(self, activities) -> None:
        self.delivered.extend(activities)


async def test_the_activity_pass_holds_a_paused_record_and_releases_it_later(tmp_path) -> None:
    """End to end from the spool: nothing on the pass that drains it, a finish once it is quiet."""
    spool = tmp_path / "activity"
    spool.mkdir()
    (spool / f"{_SESSION}-20261017T191100000001Z.json").write_text(
        json.dumps(
            {
                "session_id": _SESSION,
                "event": "Stop",
                "reason": None,
                "detail": "The shell is running.",
                "observed_at": _PAUSED_AT.isoformat(),
                "ask": None,
                "paused": True,
            }
        ),
        encoding="utf-8",
    )
    notifier = _Notifier()
    composition = ServiceComposition(
        SimpleNamespace(notifier=notifier), None, None, activity_directory=spool
    )

    await _watch_activity_once(composition)
    assert notifier.delivered == []

    held, _since = composition.paused_turns[_SESSION]
    composition.paused_turns[_SESSION] = (held, time.monotonic() - _PAUSED_QUIET_SECONDS)
    await _watch_activity_once(composition)

    (finished,) = notifier.delivered
    assert finished.kind is ActivityKind.COMPLETED and not finished.paused


async def test_a_session_closed_while_its_finish_is_held_gets_no_feed_row(tmp_path) -> None:
    """The release passes the same liveness gate as any activity (DEC-031), so a session the
    owner closed during the quiet spell is not told "finished" in the feed. The bot declines it
    at send time too (`_display_for`; `test_live_service.py`'s
    `test_a_session_that_stops_while_its_notification_waits_is_not_notified`)."""
    connection = open_database(tmp_path / "state.sqlite3", migrations=MIGRATIONS)
    live = {_SESSION: True}

    async def live_session(session_id: str) -> bool:
        return live.get(session_id, False)

    composition = ServiceComposition(
        SimpleNamespace(notifier=_Notifier()),
        None,
        None,
        activity_store=SQLiteActivityStore(connection),
        live_session=live_session,
    )
    composition.paused_turns[_SESSION] = (
        _activity(paused=True),
        time.monotonic() - _PAUSED_QUIET_SECONDS,
    )
    live[_SESSION] = False

    await _watch_activity_once(composition)

    assert composition.paused_turns == {}
    assert await SQLiteActivityStore(connection).recent(limit=5) == ()
    connection.close()
