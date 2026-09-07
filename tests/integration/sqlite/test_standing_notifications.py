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


def test_the_class_of_ask_survives_the_round_trip(tmp_path) -> None:
    """A replacement message must say what the first one said.

    The first message a session sends is rendered from live `AgentActivity` values, so it names
    the class of ask. A replacement is rebuilt from this table — which is the common case, since
    replacing is the whole design of a standing notification — and until 2026-09-07 the snapshot
    did not carry `ask`, so the wording silently downgraded from "Waiting for an answer about a
    shell command" to "Waiting for an answer" the moment a second report arrived.

    Invisible until Stage 5's live drill, because the running service predated the `ask` column
    and no ask had ever reached this table to be dropped.
    """
    asked = AgentActivity(
        _SESSION, ActivityKind.NEEDS_ANSWER, None, _OBSERVED, ActivityConfidence.REPORTED, "bash"
    )
    database = tmp_path / "sessions.sqlite3"

    connection = open_database(database)
    SQLiteStandingNotificationStore(connection).record(_CHAT, _notification(asked))
    connection.close()

    connection = open_database(database)
    standing = SQLiteStandingNotificationStore(connection).notification(_CHAT, _SESSION)
    connection.close()

    assert standing is not None
    assert standing.activities[0].ask == "bash"


def test_a_row_written_before_the_ask_key_existed_is_still_readable(tmp_path) -> None:
    """An upgrade must not restart every notification that was mid-flight across it.

    Subscripting the key would send such a row down the "this build cannot read it" path, which
    drops the record of the message and starts a new one — a duplicate notification per live
    session, once, on upgrade. `.get` reads the absence as what it meant: no ask.
    """
    import json

    database = tmp_path / "sessions.sqlite3"
    connection = open_database(database)
    SQLiteStandingNotificationStore(connection).record(_CHAT, _notification(_activity()))
    stored = connection.execute("SELECT activities FROM standing_notifications").fetchone()[0]
    lines = json.loads(stored)
    for line in lines:
        line.pop("ask")
    connection.execute("UPDATE standing_notifications SET activities = ?", (json.dumps(lines),))
    connection.commit()

    standing = SQLiteStandingNotificationStore(connection).notification(_CHAT, _SESSION)
    connection.close()

    assert standing is not None, "a pre-upgrade row must be read, not dropped"
    assert standing.activities[0].ask is None


def test_every_rendered_field_of_an_observation_round_trips() -> None:
    """Structural, because the rule is "all of them" and a case can only check the ones it names.

    `ask` was missing from the snapshot for a week and nothing failed; what was missing was not a
    test for `ask` but a test for the *rule*. A seventh field on `AgentActivity` now has to be
    either stored here or deliberately excluded, and excluding it is an edit somebody makes on
    purpose rather than by not thinking about it.
    """
    import dataclasses
    import re
    from pathlib import Path

    from remote_agents.adapters.sqlite import standing_notification_store

    source = Path(standing_notification_store.__file__).read_text(encoding="utf-8")
    stored = set(re.findall(r'"(\w+)": activity\.', source))
    fields = {field.name for field in dataclasses.fields(AgentActivity)}
    # The session id is the row's key and is deliberately not repeated per line.
    assert fields - stored == {"session_id"}, (
        f"{fields - stored - {'session_id'}} is on AgentActivity and not in the snapshot; a "
        "replacement message would render it as absent"
    )
