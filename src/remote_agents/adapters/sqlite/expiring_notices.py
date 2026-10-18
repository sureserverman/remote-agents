"""SQLite home for the notifications that remove themselves (UI migration 3)."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from remote_agents.ports.expiring_notices import ExpiringNotice


class SQLiteExpiringNoticeStore:
    """One row per sent message, deleted with the message.

    Writes go through `with self._connection:`, the house idiom the sibling stores use: it
    rolls back on any exception rather than leaving a transaction open on a shared connection.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    async def remember(self, notice: ExpiringNotice) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO expiring_notices"
                "(chat_id, message_id, kind, subject, expires_at) VALUES (?, ?, ?, ?, ?)",
                (
                    notice.chat_id,
                    notice.message_id,
                    notice.kind,
                    notice.subject,
                    notice.expires_at.isoformat(),
                ),
            )

    async def standing(self, kind: str) -> tuple[ExpiringNotice, ...]:
        rows = self._connection.execute(
            "SELECT chat_id, message_id, kind, subject, expires_at FROM expiring_notices"
            " WHERE kind = ? ORDER BY rowid",
            (kind,),
        ).fetchall()
        return tuple(
            ExpiringNotice(row[0], row[1], row[2], row[3], datetime.fromisoformat(row[4]))
            for row in rows
        )

    async def forget(self, chat_id: int, message_id: int) -> None:
        with self._connection:
            self._connection.execute(
                "DELETE FROM expiring_notices WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            )
