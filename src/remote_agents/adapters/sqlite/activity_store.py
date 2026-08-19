"""Durable, append-only record of agent observations, read newest-first for the feed.

Deliberately not a delivery ledger: DEC-026 keeps the Telegram notifier's queue and rate
state in memory, and nothing here changes that — a row says "this was observed", never
"this was delivered". The reader is bounded because a feed is a glance, not an archive.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind, AgentActivity

_LOG = logging.getLogger(__name__)


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
                    # Deliberately not persisted. DEC-013: nothing a payload carries is
                    # stored — agent-reported content reaches presentation only. The kind,
                    # session, time, and confidence are this project's own vocabulary; the
                    # detail is the agent's words, rendered by the live pass and then gone.
                    # Whether the durable feed should carry it is the owner's decision to
                    # take (it needs a DEC-013 supersede), recorded in the backlog.
                    None,
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
        activities = []
        for row in rows:
            try:
                activities.append(
                    AgentActivity(
                        row[0],
                        ActivityKind(row[1]),
                        row[2],
                        _instant(row[4]),
                        ActivityConfidence(row[3]),
                    )
                )
            except ValueError:
                # A row written under a vocabulary this build no longer speaks — the enum
                # docstrings call retiring a kind an expected evolution. One poisoned row
                # costs itself, never the whole glance.
                _LOG.warning("skipping an activity row with an unknown kind or confidence")
        return tuple(activities)


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
