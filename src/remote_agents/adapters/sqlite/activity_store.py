"""Durable, append-only record of agent observations, read newest-first for the feed.

Deliberately not a delivery ledger: DEC-026 keeps the Telegram notifier's queue and rate
state in memory, and nothing here changes that — a row says "this was observed", never
"this was delivered". The reader is bounded because a feed is a glance, not an archive.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind, AgentActivity


class SQLiteActivityStore:
    """Append observations and read the newest few; nothing here is ever updated."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    async def append(self, activity: AgentActivity) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO agent_activity(session_id, kind, detail, confidence, observed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    activity.session_id,
                    activity.kind.value,
                    activity.detail,
                    activity.confidence.value,
                    activity.observed_at.astimezone(UTC).isoformat(),
                ),
            )

    async def recent(self, *, limit: int) -> tuple[AgentActivity, ...]:
        """The newest `limit` observations, newest first, by insertion order."""
        if limit < 1:
            raise ValueError("the feed reads at least one row")
        rows = self._connection.execute(
            """
            SELECT session_id, kind, detail, confidence, observed_at
            FROM agent_activity ORDER BY activity_id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return tuple(
            AgentActivity(
                row[0],
                ActivityKind(row[1]),
                row[2],
                _instant(row[4]),
                ActivityConfidence(row[3]),
            )
            for row in rows
        )


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
