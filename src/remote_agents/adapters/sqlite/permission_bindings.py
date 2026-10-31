"""Launch permission snapshots, stored outside positional session projections."""

import sqlite3

from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import ProfileId, SessionId
from remote_agents.domain.profiles import PermissionPreset


class SQLitePermissionBindings:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    async def for_session(self, session_id: SessionId) -> PermissionPreset | None:
        row = self._connection.execute(
            "SELECT preset FROM session_permissions WHERE session_id = ?",
            (str(session_id),),
        ).fetchone()
        return None if row is None else PermissionPreset(row[0])

    async def for_conversation(
        self,
        profile_id: ProfileId,
        source_id: str,
    ) -> PermissionPreset | None:
        row = self._connection.execute(
            """SELECT p.preset FROM session_permissions p
               JOIN sessions s ON s.session_id = p.session_id
               WHERE s.profile_id = ? AND (p.conversation_id = ? OR s.resume_source_id = ?)
               ORDER BY s.created_at DESC, s.rowid DESC LIMIT 1""",
            (str(profile_id), source_id, source_id),
        ).fetchone()
        return None if row is None else PermissionPreset(row[0])

    async def remember(self, session_id: SessionId, source_id: str) -> None:
        source_id = ProviderConversationId(source_id).value
        with self._connection:
            self._connection.execute(
                """UPDATE session_permissions SET conversation_id = ?
                   WHERE session_id = ? AND conversation_id IS NULL""",
                (source_id, str(session_id)),
            )
