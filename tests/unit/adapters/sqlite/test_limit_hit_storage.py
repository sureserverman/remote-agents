"""A limit stop keeps its window and reset instant through both stores that hold activities.

`agent_activity` is the feed's append-only record and `standing_notifications` is the bot's copy
of what is on screen. A limit stop is only useful to the later stages -- the bot's line retiring
when the limit lifts, and the "carry on" after it -- if the window and the instant survive both,
including a restart. What is kept is a provider label and a provider instant, never agent words
(DEC-013/037 are unchanged by this).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.sqlite.database import open_database, open_ui_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.standing_notification_store import (
    SQLiteStandingNotificationStore,
)
from remote_agents.ports.agent_activity import (
    ActivityConfidence,
    ActivityKind,
    AgentActivity,
    LimitHit,
)
from remote_agents.ports.standing_notification import StandingNotification

_SESSION = "7a729881-8115-41fb-8613-160182188f40"
_CHAT = 11
_OBSERVED = datetime(2026, 9, 28, 21, 13, tzinfo=UTC)
_RESETS = datetime(2026, 9, 28, 23, 50, tzinfo=UTC)


def _stop(limit: LimitHit | None) -> AgentActivity:
    return AgentActivity(
        _SESSION,
        ActivityKind.LIMIT_REACHED,
        "You've hit your session limit · resets 10:50pm (Europe/London)",
        _OBSERVED,
        ActivityConfidence.REPORTED,
        limit=limit,
    )


async def test_the_activity_store_round_trips_a_limit_hit(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "state.sqlite3", migrations=MIGRATIONS)
    try:
        store = SQLiteActivityStore(connection)
        await store.append(_stop(LimitHit("5h", _RESETS)))
        await store.append(_stop(LimitHit(None, None)))
        await store.append(_stop(None))
        await store.append(AgentActivity(_SESSION, ActivityKind.COMPLETED, None, _OBSERVED))

        other, unset, blind, known = await store.recent(limit=4)
    finally:
        connection.close()

    assert known.limit == LimitHit("5h", _RESETS)
    # A stop whose window could not be named is still a stop: it reads back as a hit with
    # nothing known. A limit stop recorded with no hit at all means the same thing, so the two
    # read back alike -- the store never answers "not a limit stop" for a `LIMIT_REACHED`.
    assert blind.limit == LimitHit(None, None)
    assert unset.limit == LimitHit(None, None)
    # Every other kind carries no hit.
    assert other.limit is None


async def test_a_limit_hit_survives_the_migration_of_a_pre_existing_store(tmp_path: Path) -> None:
    """The operator's live database gains the columns; an old stop reads back with nothing known."""
    path = tmp_path / "state.sqlite3"
    old = open_database(path, migrations=MIGRATIONS[:-1])
    old.execute(
        "INSERT INTO agent_activity(session_id, kind, detail, confidence, observed_at, ask)"
        " VALUES (?, 'limit_reached', NULL, 'reported', ?, NULL)",
        (_SESSION, _OBSERVED.isoformat()),
    )
    old.commit()
    old.close()

    connection = open_database(path, migrations=MIGRATIONS)
    try:
        store = SQLiteActivityStore(connection)
        await store.append(_stop(LimitHit("week", _RESETS)))
        newest, oldest = await store.recent(limit=2)
    finally:
        connection.close()

    assert oldest.kind is ActivityKind.LIMIT_REACHED
    # Nothing measured the window of a stop recorded before the columns existed.
    assert oldest.limit == LimitHit(None, None)
    assert newest.limit == LimitHit("week", _RESETS)


def test_the_standing_store_round_trips_a_limit_hit_across_a_restart(tmp_path: Path) -> None:
    database = tmp_path / "ui.sqlite3"
    first = open_ui_database(database)
    SQLiteStandingNotificationStore(first).record(
        _CHAT,
        StandingNotification(_SESSION, 1104, (_stop(LimitHit("month", _RESETS)),), "c1_token"),
    )
    first.close()

    recalled = SQLiteStandingNotificationStore(open_ui_database(database)).notification(
        _CHAT, _SESSION
    )

    assert recalled is not None
    (line,) = recalled.activities
    assert line.limit == LimitHit("month", _RESETS)


def test_a_standing_row_written_before_the_limit_key_still_loads(tmp_path: Path) -> None:
    """A message mid-flight across the upgrade keeps its record rather than being re-sent."""
    connection = open_ui_database(tmp_path / "ui.sqlite3")
    connection.execute(
        "INSERT INTO standing_notifications"
        "(chat_id, session_id, message_id, token, activities, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            _CHAT,
            _SESSION,
            1104,
            "c1_token",
            json.dumps(
                [
                    {
                        "kind": "limit_reached",
                        "detail": None,
                        "confidence": "reported",
                        "observed_at": _OBSERVED.isoformat(),
                        "ask": None,
                    }
                ]
            ),
            _OBSERVED.isoformat(),
        ),
    )
    connection.commit()

    recalled = SQLiteStandingNotificationStore(connection).notification(_CHAT, _SESSION)

    assert recalled is not None
    (line,) = recalled.activities
    assert line.kind is ActivityKind.LIMIT_REACHED
    assert line.limit == LimitHit(None, None)
