"""Durable record of which message a chat's live view is, so a restart redraws it."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


class SQLiteChatViewStore:
    """Remember the one message this chat's screens are drawn into.

    Durability is the point rather than a bonus. A restart that forgot the anchor would
    send a *second* live view and leave the first sitting above it holding buttons that
    still resolve — the durable callback states from Stage 1 would keep that stale screen
    alive indefinitely, which is precisely the transcript this stage removes.

    `updated_at` is recorded and never read back. It exists so an operator reading the
    database can tell a live anchor from one left by a deployment weeks ago.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def anchor(self, chat_id: int) -> int | None:
        row = self._connection.execute(
            "SELECT message_id FROM chat_views WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return None if row is None else int(row[0])

    def record_anchor(self, chat_id: int, message_id: int) -> None:
        if message_id <= 0:
            raise ValueError("a live view must be anchored to a real Telegram message")
        with self._connection:
            self._connection.execute(
                "INSERT INTO chat_views(chat_id, message_id, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET message_id = excluded.message_id, "
                "updated_at = excluded.updated_at",
                (chat_id, message_id, datetime.now(UTC).isoformat()),
            )

    def adopt_anchor(self, chat_id: int, message_id: int) -> bool:
        """Claim the anchor for a chat that has none, in one statement.

        `DO NOTHING` is what makes this safe under DEC-005's second writer: the conditional
        lives in SQLite rather than between two of our round trips, so the loser of the race
        is told it lost instead of silently overwriting the winner.
        """
        if message_id <= 0:
            raise ValueError("a live view must be anchored to a real Telegram message")
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO chat_views(chat_id, message_id, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id) DO NOTHING",
                (chat_id, message_id, datetime.now(UTC).isoformat()),
            )
        return cursor.rowcount == 1

    def enqueue_cleanup(self, chat_id: int, message_id: int, kind: str) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO retired_messages(chat_id,message_id,kind) VALUES(?,?,?)",
                (chat_id, message_id, kind),
            )

    def due_cleanup(self, chat_id: int, now: float) -> tuple[tuple[int, str, int], ...]:
        return tuple(
            tuple(row)
            for row in self._connection.execute(
                "SELECT message_id,kind,attempts FROM retired_messages "
                "WHERE chat_id=? AND due<=? ORDER BY due,message_id LIMIT 32",
                (chat_id, now),
            )
        )

    def finish_cleanup(self, chat_id: int, message_id: int) -> None:
        with self._connection:
            self._connection.execute(
                "DELETE FROM retired_messages WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            )

    def defer_cleanup(self, chat_id: int, message_id: int, due: float) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE retired_messages SET attempts=attempts+1, due=? "
                "WHERE chat_id=? AND message_id=?",
                (due, chat_id, message_id),
            )
