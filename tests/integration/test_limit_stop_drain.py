"""A drained Claude limit stop is classified before it is recorded or delivered.

`StopFailure(error=rate_limit)` says only "rate limit". The window comes from the classifier,
which the service's activity pass runs over what it drained: the reading names a saturated
window, and Claude's own sentence names one otherwise. Recorded *and* delivered with it, because
the feed row, the bot line and the later lift all read the same activity.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from remote_agents.adapters.agents.claude.limit_screen import LIMIT_SCREEN
from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.application.limit_stops import LimitStopClassifier
from remote_agents.composition.service import ServiceComposition, _watch_activity_once
from remote_agents.domain.models import ProfileId, SessionId
from remote_agents.ports.agent_activity import ActivityKind, LimitHit
from remote_agents.ports.agent_usage import AgentLimits, UsageWindow

_SESSION = "7a729881-8115-41fb-8613-160182188f40"
_NOW = datetime(2026, 9, 24, 7, 36, tzinfo=UTC)
_WEEK_RESETS = _NOW + timedelta(days=2)


class _Notifier:
    def __init__(self) -> None:
        self.delivered: list = []

    async def deliver(self, activities) -> None:
        self.delivered.extend(activities)


class _Store:
    """Only what the classifier asks of a session store: which profile a session runs."""

    def __init__(self, profile: str) -> None:
        self._profile = profile

    async def get(self, session_id: SessionId):
        assert str(session_id) == _SESSION
        return SimpleNamespace(session_id=session_id, profile_id=ProfileId(self._profile))


def _spool(directory, detail: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{_SESSION}-20260924T073546000001Z.json").write_text(
        json.dumps(
            {
                "session_id": _SESSION,
                "event": "StopFailure",
                "reason": "rate_limit",
                "observed_at": "2026-09-24T07:35:46+00:00",
                "detail": detail,
            }
        ),
        encoding="utf-8",
    )


async def _pass(tmp_path, *, readings: tuple[AgentLimits, ...], detail: str):
    spool = tmp_path / "activity"
    _spool(spool, detail)
    connection = open_database(tmp_path / "state.sqlite3", migrations=MIGRATIONS)
    notifier = _Notifier()

    async def limits() -> tuple[AgentLimits, ...]:
        return readings

    composition = ServiceComposition(
        SimpleNamespace(notifier=notifier),
        None,
        None,
        activity_directory=spool,
        activity_store=SQLiteActivityStore(connection),
        limit_classifier=LimitStopClassifier(
            _Store("claude"), limits, {"claude": LIMIT_SCREEN}, now=lambda: _NOW
        ),
    )
    await _watch_activity_once(composition)
    (stored,) = await SQLiteActivityStore(connection).recent(limit=5)
    connection.close()
    return notifier.delivered, stored


async def test_the_reading_names_the_window_of_a_drained_stop(tmp_path) -> None:
    reading = AgentLimits(
        "claude",
        (UsageWindow("5h", 40, _NOW + timedelta(hours=3)), UsageWindow("week", 100, _WEEK_RESETS)),
        observed_at=_NOW - timedelta(minutes=2),
        stale_source="status line",
    )
    delivered, stored = await _pass(
        tmp_path,
        readings=(reading,),
        detail="You've hit your session limit · resets 10:50am (Europe/London)",
    )

    (activity,) = delivered
    assert activity.kind is ActivityKind.LIMIT_REACHED
    assert activity.limit == LimitHit("week", _WEEK_RESETS)
    assert stored.limit == LimitHit("week", _WEEK_RESETS)


async def test_claude_s_sentence_names_it_when_no_reading_can(tmp_path) -> None:
    delivered, stored = await _pass(
        tmp_path,
        readings=(),
        detail="You've hit your session limit · resets 10:50am (Europe/London)",
    )

    (activity,) = delivered
    assert activity.limit is not None
    assert activity.limit.window == "5h"
    assert activity.limit.resets_at == datetime(2026, 9, 24, 9, 50, tzinfo=UTC)
    assert stored.limit == activity.limit


async def test_a_reader_that_raises_leaves_the_stop_unclassified_and_delivered(tmp_path) -> None:
    """A failing read costs the window, never the notification or the feed row."""
    spool = tmp_path / "activity"
    _spool(spool, "")
    connection = open_database(tmp_path / "state.sqlite3", migrations=MIGRATIONS)
    notifier = _Notifier()

    async def limits() -> tuple[AgentLimits, ...]:
        raise OSError("the status-line recording is unreadable")

    composition = ServiceComposition(
        SimpleNamespace(notifier=notifier),
        None,
        None,
        activity_directory=spool,
        activity_store=SQLiteActivityStore(connection),
        limit_classifier=LimitStopClassifier(
            _Store("claude"), limits, {"claude": LIMIT_SCREEN}, now=lambda: _NOW
        ),
    )
    await _watch_activity_once(composition)
    (stored,) = await SQLiteActivityStore(connection).recent(limit=5)
    connection.close()

    (activity,) = notifier.delivered
    assert activity.kind is ActivityKind.LIMIT_REACHED
    assert stored.limit == LimitHit(None, None)
