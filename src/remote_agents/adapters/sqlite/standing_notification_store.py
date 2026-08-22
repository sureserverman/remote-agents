"""Durable record of which message a session's notification is, so a restart amends it.

The sibling of :class:`~remote_agents.adapters.sqlite.chat_view_store.SQLiteChatViewStore`, and
it exists for the same reason at one level down: that one stops a restart sending a second live
view, this one stops a restart sending a second *notification*.

Deliberately not a delivery ledger. DEC-026 keeps the notifier's undelivered queue and its rate
windows in memory and nothing here changes that -- a row says "this session already owns that
message", never "this was delivered".
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime

from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind, AgentActivity
from remote_agents.ports.standing_notification import StandingNotification

_LOG = logging.getLogger(__name__)


class SQLiteStandingNotificationStore:
    """Remember one message per session, and forget it when the message leaves the chat."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def standing(self, chat_id: int) -> tuple[StandingNotification, ...]:
        rows = self._connection.execute(
            """
            SELECT session_id, message_id, token, activities
            FROM standing_notifications WHERE chat_id = ?
            ORDER BY session_id
            """,
            (chat_id,),
        ).fetchall()
        return tuple(filter(None, (_notification(row) for row in rows)))

    def notification(self, chat_id: int, session_id: str) -> StandingNotification | None:
        row = self._connection.execute(
            """
            SELECT session_id, message_id, token, activities
            FROM standing_notifications WHERE chat_id = ? AND session_id = ?
            """,
            (chat_id, session_id),
        ).fetchone()
        return None if row is None else _notification(row)

    def record(self, chat_id: int, notification: StandingNotification) -> None:
        if notification.message_id <= 0:
            raise ValueError("a standing notification must name a real Telegram message")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO standing_notifications(
                    chat_id, session_id, message_id, token, activities, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, session_id) DO UPDATE SET
                    message_id = excluded.message_id,
                    token = excluded.token,
                    activities = excluded.activities,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_id,
                    notification.session_id,
                    notification.message_id,
                    notification.token,
                    _encoded(notification.activities),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def forget(self, chat_id: int, session_id: str) -> None:
        with self._connection:
            self._connection.execute(
                "DELETE FROM standing_notifications WHERE chat_id = ? AND session_id = ?",
                (chat_id, session_id),
            )


def _encoded(activities: tuple[AgentActivity, ...]) -> str:
    """The lines a message spells out, as the JSON one column can hold.

    The session id is the row's key and is not repeated per line. The agent's own words ride
    along under DEC-037, which already persists them one table over; what is stored here is a
    copy of what is *on screen*, and it leaves with the message.
    """
    return json.dumps(
        [
            {
                "kind": activity.kind.value,
                "detail": activity.detail,
                "confidence": activity.confidence.value,
                "observed_at": activity.observed_at.astimezone(UTC).isoformat(),
            }
            for activity in activities
        ]
    )


def _notification(row: tuple) -> StandingNotification | None:
    """Rebuild one row, or answer that this build cannot read it.

    A row written under a vocabulary this build no longer speaks -- the kind enum's docstring
    calls retiring a kind an expected evolution -- must not cost the whole sweep. Answering
    None drops the *record* of the message rather than the message: the session then starts a
    new notification, which is the behaviour of every build before this table existed.
    """
    session_id, message_id, token, encoded = row
    try:
        lines = json.loads(encoded)
        activities = tuple(
            AgentActivity(
                str(session_id),
                ActivityKind(line["kind"]),
                line["detail"],
                _instant(line["observed_at"]),
                ActivityConfidence(line["confidence"]),
            )
            for line in lines
        )
    except (ValueError, TypeError, KeyError):
        _LOG.warning("dropping a standing notification this build cannot read")
        return None
    return StandingNotification(str(session_id), int(message_id), activities, str(token))


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
