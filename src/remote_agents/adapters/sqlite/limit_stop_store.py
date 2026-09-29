"""Which limit stops are still undecided, and what became of the ones that are not (migration 16).

Reads `agent_activity` -- the append-only record the feed reads -- and writes only
`limit_stop_outcomes`, so recording that a stop lifted can never touch what was observed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from datetime import UTC, datetime

from remote_agents.adapters.sqlite.activity_store import _instant, _limit
from remote_agents.ports.agent_activity import ActivityKind
from remote_agents.ports.limit_stop_outcomes import NUDGING, RESUMED, LimitStop


class SQLiteLimitStopStore:
    """`LimitStopOutcomes` over the domain database the activity store writes."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    async def unresolved(self, session_ids: Collection[str]) -> tuple[LimitStop, ...]:
        """Each session whose newest observation, by insertion, is an undecided limit stop.

        Newest by `activity_id`, the order the feed reads in, rather than by stamp: a stop is
        superseded by whatever the service recorded after it, which is also what retired its
        line from the bot's message (limit-lifecycle sub-plan 2, Stage 1).
        """
        stops: list[LimitStop] = []
        for session_id in session_ids:
            row = self._connection.execute(
                """
                SELECT a.observed_at, a.limit_window, a.limit_resets_at
                FROM agent_activity AS a
                WHERE a.activity_id = (
                    SELECT MAX(activity_id) FROM agent_activity WHERE session_id = ?
                )
                AND a.kind = ?
                AND NOT EXISTS (
                    SELECT 1 FROM limit_stop_outcomes AS o
                    WHERE o.session_id = a.session_id AND o.stopped_at = a.observed_at
                )
                """,
                (session_id, ActivityKind.LIMIT_REACHED.value),
            ).fetchone()
            if row is None:
                continue
            hit = _limit(ActivityKind.LIMIT_REACHED, row[1], row[2])
            stops.append(LimitStop(session_id, _instant(row[0]), hit, row[0]))
        return tuple(stops)

    async def record(self, stop: LimitStop, outcome: str, *, decided_at: datetime) -> bool:
        """Keyed on the stamp exactly as `agent_activity` stored it, so the two rows join.

        The first outcome stands, except over a `NUDGING` intent, which it replaces.
        """
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO limit_stop_outcomes(session_id, stopped_at, outcome, decided_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, stopped_at) DO UPDATE
                SET outcome = excluded.outcome, decided_at = excluded.decided_at
                WHERE limit_stop_outcomes.outcome = ?
                """,
                (
                    stop.session_id,
                    stop.stamp,
                    outcome,
                    decided_at.astimezone(UTC).isoformat(),
                    NUDGING,
                ),
            )
        return cursor.rowcount > 0

    async def claim(self, stop: LimitStop, *, decided_at: datetime) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO limit_stop_outcomes(
                    session_id, stopped_at, outcome, decided_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (stop.session_id, stop.stamp, NUDGING, decided_at.astimezone(UTC).isoformat()),
            )
        return cursor.rowcount > 0

    async def release(self, stop: LimitStop) -> None:
        with self._connection:
            self._connection.execute(
                "DELETE FROM limit_stop_outcomes"
                " WHERE session_id = ? AND stopped_at = ? AND outcome = ?",
                (stop.session_id, stop.stamp, NUDGING),
            )

    async def interrupted(self) -> tuple[LimitStop, ...]:
        """Each `NUDGING` intent, joined back to the stop it was written for."""
        rows = self._connection.execute(
            """
            SELECT o.session_id, o.stopped_at, a.limit_window, a.limit_resets_at
            FROM limit_stop_outcomes AS o
            JOIN agent_activity AS a
              ON a.session_id = o.session_id AND a.observed_at = o.stopped_at AND a.kind = ?
            WHERE o.outcome = ?
            GROUP BY o.session_id, o.stopped_at
            """,
            (ActivityKind.LIMIT_REACHED.value, NUDGING),
        ).fetchall()
        return tuple(
            LimitStop(
                session_id,
                _instant(stamp),
                _limit(ActivityKind.LIMIT_REACHED, window, resets_at),
                stamp,
            )
            for session_id, stamp, window, resets_at in rows
        )

    async def last_resumed_at(self, session_id: str) -> datetime | None:
        """The newest `RESUMED` decision for the session, read back as an instant."""
        row = self._connection.execute(
            """
            SELECT MAX(decided_at) FROM limit_stop_outcomes
            WHERE session_id = ? AND outcome = ?
            """,
            (session_id, RESUMED),
        ).fetchone()
        return None if row is None or row[0] is None else _instant(row[0])
