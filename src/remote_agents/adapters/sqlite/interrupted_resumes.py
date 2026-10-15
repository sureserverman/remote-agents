"""What the startup resume pass decided for each interrupted session (migration 20)."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime

from remote_agents.ports.interrupted_resumes import NUDGING, RESUMING


class SQLiteInterruptedResumes:
    """`InterruptedResumes` over the domain database."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    async def outcomes(self) -> Mapping[str, str]:
        rows = self._connection.execute(
            "SELECT session_id, outcome FROM interrupted_resumes"
        ).fetchall()
        return {str(session_id): str(outcome) for session_id, outcome in rows}

    async def record(self, session_id: str, outcome: str, *, decided_at: datetime) -> bool:
        """The first outcome stands, except over an intent. `RESUMING` yields to anything;
        `nudging:` yields only to a final outcome, so it can never step back to an intent."""
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO interrupted_resumes(session_id, outcome, decided_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE
                SET outcome = excluded.outcome, decided_at = excluded.decided_at
                WHERE interrupted_resumes.outcome = ?
                OR (
                    substr(interrupted_resumes.outcome, 1, ?) = ?
                    AND excluded.outcome <> ?
                    AND substr(excluded.outcome, 1, ?) <> ?
                )
                """,
                (
                    session_id,
                    outcome,
                    decided_at.astimezone(UTC).isoformat(),
                    RESUMING,
                    len(NUDGING) + 1,
                    f"{NUDGING}:",
                    RESUMING,
                    len(NUDGING) + 1,
                    f"{NUDGING}:",
                ),
            )
        return cursor.rowcount > 0
