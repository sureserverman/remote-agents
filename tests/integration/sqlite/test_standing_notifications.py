"""The standing notification is durable, so a restart amends its message instead of adding one.

The sibling of `test_chat_view.py` one level down: that one keeps a restart from sending a
second live view, this one keeps it from sending a second *notification*. Composed the way
`bootstrap.main` composes it — the first connection is closed before the second is opened, so
nothing here is proved by a shared handle to an already-open file.
"""

from datetime import UTC, datetime

import pytest

from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.standing_notification_store import (
    SQLiteStandingNotificationStore,
)
from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind, AgentActivity
from remote_agents.ports.standing_notification import StandingNotification

_CHAT = 11
_SESSION = "7a729881-8115-41fb-8613-160182188f40"
_OBSERVED = datetime(2026, 8, 20, 21, 13, tzinfo=UTC)


def _activity(
    kind: ActivityKind = ActivityKind.COMPLETED, detail: str | None = None
) -> AgentActivity:
    return AgentActivity(_SESSION, kind, detail, _OBSERVED, ActivityConfidence.REPORTED)


def _notification(*activities: AgentActivity) -> StandingNotification:
    return StandingNotification(_SESSION, 1104, activities or (_activity(),), "c1_open_token")


def test_a_session_with_no_notification_yet_has_none(tmp_path) -> None:
    store = SQLiteStandingNotificationStore(open_database(tmp_path / "sessions.sqlite3"))

    assert store.notification(_CHAT, _SESSION) is None
    assert store.standing(_CHAT) == ()


def test_a_notification_survives_the_connection_that_recorded_it(tmp_path) -> None:
    """The restart property, and the whole reason this table exists."""
    database = tmp_path / "sessions.sqlite3"
    first = open_database(database)
    SQLiteStandingNotificationStore(first).record(_CHAT, _notification())
    first.close()

    recalled = SQLiteStandingNotificationStore(open_database(database)).notification(
        _CHAT, _SESSION
    )

    assert recalled is not None
    assert recalled.message_id == 1104
    assert recalled.token == "c1_open_token"


def test_the_lines_the_message_spells_out_come_back_whole(tmp_path) -> None:
    """An amendment after a restart has to say "finished, then asked a question" — carrying
    only the newest arrival would silently delete agent output the drain has already removed
    from disk."""
    database = tmp_path / "sessions.sqlite3"
    first = open_database(database)
    told = (_activity(detail="Found it."), _activity(ActivityKind.NEEDS_ANSWER))
    SQLiteStandingNotificationStore(first).record(_CHAT, _notification(*told))
    first.close()

    recalled = SQLiteStandingNotificationStore(open_database(database)).notification(
        _CHAT, _SESSION
    )

    assert recalled is not None
    assert recalled.activities == told


def test_recording_again_moves_the_message_rather_than_keeping_two(tmp_path) -> None:
    """One session, one notification — the primary key is the invariant being stored."""
    connection = open_database(tmp_path / "sessions.sqlite3")
    store = SQLiteStandingNotificationStore(connection)
    store.record(_CHAT, _notification())

    store.record(_CHAT, StandingNotification(_SESSION, 1180, (_activity(),), "c1_moved_token"))

    held = store.standing(_CHAT)
    assert len(held) == 1
    assert held[0].message_id == 1180
    assert connection.execute("SELECT COUNT(*) FROM standing_notifications").fetchone()[0] == 1


def test_forgetting_leaves_the_other_sessions_standing(tmp_path) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")
    store = SQLiteStandingNotificationStore(connection)
    other = "51b582fd-68b5-4c52-afcd-9d5bf77bd2b6"
    store.record(_CHAT, _notification())
    store.record(_CHAT, StandingNotification(other, 1181, (), "c1_other_token"))

    store.forget(_CHAT, _SESSION)

    assert store.notification(_CHAT, _SESSION) is None
    assert store.notification(_CHAT, other) is not None


def test_a_message_id_that_is_not_a_message_is_refused(tmp_path) -> None:
    store = SQLiteStandingNotificationStore(open_database(tmp_path / "sessions.sqlite3"))

    with pytest.raises(ValueError):
        store.record(_CHAT, StandingNotification(_SESSION, 0, (), "c1_open_token"))


@pytest.mark.asyncio
async def test_retiring_a_notification_leaves_the_feed_its_observation(tmp_path) -> None:
    """The owner asked for the finished session's *alert* to go, not its history.

    Two tables, two surfaces: the bot reads `standing_notifications` to know what is in the
    chat, and the local feed reads `agent_activity` to show what agents have been doing. This
    pins the seam, because the cheap way to make an obsolete notification disappear — deleting
    the observation behind it — would empty the feed as a side effect and nothing in the
    Telegram tests would notice.
    """
    connection = open_database(tmp_path / "sessions.sqlite3")
    feed = SQLiteActivityStore(connection)
    standing = SQLiteStandingNotificationStore(connection)
    await feed.append(_activity(detail="Found it."))
    standing.record(_CHAT, _notification())

    standing.forget(_CHAT, _SESSION)

    assert standing.notification(_CHAT, _SESSION) is None
    assert [one.detail for one in await feed.recent(limit=10)] == ["Found it."]
