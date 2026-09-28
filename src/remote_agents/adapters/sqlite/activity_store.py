"""Durable, append-only record of agent observations, read newest-first for the feed.

Deliberately not a delivery ledger: DEC-026 keeps the Telegram notifier's queue and rate
state in memory, and nothing here changes that — a row says "this was observed", never
"this was delivered". The reader is bounded because a feed is a glance, not an archive.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from remote_agents.ports.agent_activity import (
    ActivityConfidence,
    ActivityKind,
    AgentActivity,
    LimitHit,
)

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
                INSERT INTO agent_activity(
                    session_id, kind, detail, confidence, observed_at, ask,
                    limit_window, limit_resets_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                    # The provider's ask token, not a class. Storing the classification would
                    # freeze today's vocabulary into every historical row, so a token later
                    # recognised would still read as unknown in the feed (DEC-074).
                    activity.ask,
                    # A provider's window label and instant, never its words (migration 15).
                    None if activity.limit is None else activity.limit.window,
                    _stored_instant(None if activity.limit is None else activity.limit.resets_at),
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
                    SELECT activity_id, session_id, kind, detail, confidence, observed_at, ask,
                        limit_window, limit_resets_at
                    FROM agent_activity ORDER BY activity_id DESC LIMIT ?
                    """,
                    (batch,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT activity_id, session_id, kind, detail, confidence, observed_at, ask,
                        limit_window, limit_resets_at
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
                            # NULL on every row written before migration 11, which is most of
                            # them and stays that way -- the column is additive and nothing
                            # backfills it, because nothing knows what those rows were asking
                            # about.
                            row[6],
                            limit=_limit(ActivityKind(row[2]), row[7], row[8]),
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


def _stored_instant(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _limit(kind: ActivityKind, window: str | None, resets_at: str | None) -> LimitHit | None:
    """The hit a row recorded, and a limit stop always has one.

    A `LIMIT_REACHED` written before migration 15 has no window anyone measured, and that is
    the same fact as a stop nothing could classify, so both read back as `LimitHit(None, None)`.
    Any other kind reads back what it recorded, which is nothing.
    """
    if window is not None or resets_at is not None:
        return LimitHit(window, None if resets_at is None else _instant(resets_at))
    return LimitHit(None, None) if kind is ActivityKind.LIMIT_REACHED else None
