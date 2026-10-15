"""`ConversationLedger` over the domain database (migration 20)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from remote_agents.ports.session_identity import safe_session_id


class SQLiteConversationLedger:
    """One row per session: its newest conversation by `seen_at`."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    async def record(self, session_id: str, conversation_id: str, *, seen_at: datetime) -> None:
        # Both ids arrive from hook payloads a different process wrote, so both are checked
        # here and not trusted from the writer (the spool's own rule for the same reason).
        session = safe_session_id(session_id)
        conversation = safe_session_id(conversation_id)
        if session is None or conversation is None:
            return
        stamp = seen_at.astimezone(UTC).isoformat()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO session_conversations(session_id, provider_conversation_id, seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE
                SET provider_conversation_id = excluded.provider_conversation_id,
                    seen_at = excluded.seen_at
                WHERE excluded.seen_at >= session_conversations.seen_at
                """,
                (session, conversation, stamp),
            )

    async def latest(self, session_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT provider_conversation_id FROM session_conversations WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return None if row is None else str(row[0])
