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


#: How many extra rows a page asks for beyond what it still needs, so the common case — a
#: handful of unreadable rows among readable ones — is answered in one query rather than two.
_OVERFETCH = 8

#: The ceiling on one `recent` call's sweep. A database whose recent history is *entirely*
#: unreadable must cost a bounded read per repaint, not a full table scan: the feed repaints on
#: a timer, so an unbounded loop here would turn one bad upgrade into a permanent load problem.
#: Reaching it means returning fewer rows than asked for, which is the honest answer at that
#: point — there genuinely are not that many readable observations within reach.
_MAXIMUM_SCAN = 500


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
                    # The agent's own words, persisted deliberately (DEC-037, the owner's
                    # decision closing BL-005 — it supersedes exactly the storage clause of
                    # DEC-013). Bounded once at the drain (`bounded_detail_line`), rendered
                    # inert at the feed, never reaching the status flash; retention is
                    # indefinite by migration 9's own never-delete invariant, and that pair
                    # is DEC-037's stated, accepted cost.
                    activity.detail,
                    activity.confidence.value,
                    activity.observed_at.astimezone(UTC).isoformat(),
                ),
            )

    async def recent(self, *, limit: int) -> tuple[AgentActivity, ...]:
        """The newest `limit` observations, newest first, by insertion order.

        **Paged, because `LIMIT n` then filter is not "the newest n".** A row written under a
        vocabulary this build no longer speaks is skipped below, and a single query asking the
        database for exactly `limit` rows has already thrown away the older rows that should
        have taken their places. The glance then shows fewer than it asked for and says nothing
        about why -- which reads as "nothing else happened" rather than "something was hidden".

        Not hypothetical, and not a cost only a future retirement pays: retiring `quiet` on
        2026-08-30 left 123 unreadable rows in the owner's own database, two of them inside the
        newest fifty, against a feed that asks for twenty. The pattern predates that change --
        `ended` was retired the same way -- so this is the first read that actually notices.

        So it pages backwards by `activity_id` until it has `limit` readable rows or the table
        runs out, bounded by `_MAXIMUM_SCAN` so a table whose recent history is entirely
        unreadable costs one bounded sweep rather than a full scan on every repaint.
        """
        if limit < 1:
            raise ValueError("the feed reads at least one row")
        activities: list[AgentActivity] = []
        before: int | None = None
        scanned = 0
        while len(activities) < limit and scanned < _MAXIMUM_SCAN:
            batch = min(limit - len(activities) + _OVERFETCH, _MAXIMUM_SCAN - scanned)
            if before is None:
                rows = self._connection.execute(
                    """
                    SELECT activity_id, session_id, kind, detail, confidence, observed_at
                    FROM agent_activity ORDER BY activity_id DESC LIMIT ?
                    """,
                    (batch,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT activity_id, session_id, kind, detail, confidence, observed_at
                    FROM agent_activity WHERE activity_id < ?
                    ORDER BY activity_id DESC LIMIT ?
                    """,
                    (before, batch),
                ).fetchall()
            if not rows:
                break
            scanned += len(rows)
            before = rows[-1][0]
            for row in rows:
                if len(activities) == limit:
                    break
                try:
                    activities.append(
                        AgentActivity(
                            row[1],
                            ActivityKind(row[2]),
                            row[3],
                            _instant(row[5]),
                            ActivityConfidence(row[4]),
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
