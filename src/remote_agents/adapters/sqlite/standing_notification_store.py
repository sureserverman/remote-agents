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

from remote_agents.ports.agent_activity import (
    ActivityConfidence,
    ActivityKind,
    AgentActivity,
    LimitHit,
)
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

    **`ask` was missing here until 2026-09-07, and its absence was invisible in exactly the way
    that matters.** The *first* message is rendered from live `AgentActivity` values, so it names
    the class of ask correctly; a *replacement* is rebuilt from this snapshot, so the wording
    silently downgraded from "Waiting for an answer about a shell command" to "Waiting for an
    answer" the moment a second report arrived — and a replacement is the common case, since that
    is the whole design of a standing notification. Found by Stage 5's live drill, which is the
    first time an `ask` had survived far enough to be dropped: the running service predated the
    `ask` column, so before that it never reached this table at all.

    Everything on `AgentActivity` that a surface renders must round-trip here. That is the rule
    this field was missing from, not a list to append to.
    """
    return json.dumps(
        [
            {
                "kind": activity.kind.value,
                "detail": activity.detail,
                "confidence": activity.confidence.value,
                "observed_at": activity.observed_at.astimezone(UTC).isoformat(),
                "ask": activity.ask,
                "limit": _encoded_limit(activity.limit),
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
                # `.get`, not `[...]`: rows written before this key existed are readable and
                # simply carry no ask, which is what they meant. Subscripting would send every
                # one of them down the "this build cannot read it" path below and silently
                # restart notifications that were mid-flight across the upgrade.
                line.get("ask"),
                limit=_decoded_limit(ActivityKind(line["kind"]), line.get("limit")),
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


def _encoded_limit(limit: LimitHit | None) -> dict | None:
    if limit is None:
        return None
    resets_at = limit.resets_at
    if resets_at is not None and resets_at.tzinfo is None:
        # Read as UTC, which is what `_instant` assumes reading it back.
        resets_at = resets_at.replace(tzinfo=UTC)
    return {
        "window": limit.window,
        "resets_at": None if resets_at is None else resets_at.astimezone(UTC).isoformat(),
    }


def _decoded_limit(kind: ActivityKind, encoded: object) -> LimitHit | None:
    """The same answer the activity store gives: what was recorded, and a limit stop always has one.

    `.get`, not `[...]`, for the reason `ask` gives above: a row written before this key existed
    is readable and simply names no window.
    """
    if isinstance(encoded, dict):
        resets_at = encoded.get("resets_at")
        return LimitHit(encoded.get("window"), None if resets_at is None else _instant(resets_at))
    return LimitHit(None, None) if kind is ActivityKind.LIMIT_REACHED else None
