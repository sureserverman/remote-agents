"""Launch permission snapshots and their apply state, outside positional session projections."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import ProfileId, SessionId
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.ports.permission_bindings import PermissionState


class SQLitePermissionBindings:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._connection = connection
        self._clock = clock

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

    async def requested(self, session_id: SessionId) -> PermissionPreset | None:
        state = await self.state(session_id)
        return None if state is None else state.requested

    async def state(self, session_id: SessionId) -> PermissionState | None:
        row = self._connection.execute(
            """SELECT requested, applied, daemon, failure FROM session_permission_state
               WHERE session_id = ?""",
            (str(session_id),),
        ).fetchone()
        if row is None:
            return None
        return PermissionState(
            requested=PermissionPreset(row[0]),
            applied=None if row[1] is None else PermissionPreset(row[1]),
            daemon=row[2],
            failure=row[3],
        )

    async def mark_applied(
        self, session_id: SessionId, preset: PermissionPreset, daemon: str
    ) -> None:
        # An UPDATE, never an upsert: a session with no row was launched by an older build.
        with self._connection:
            self._connection.execute(
                """UPDATE session_permission_state
                   SET applied = ?, daemon = ?, failure = NULL, updated_at = ?
                   WHERE session_id = ?""",
                (PermissionPreset(preset).value, daemon, self._stamp(), str(session_id)),
            )

    async def mark_failed(self, session_id: SessionId, reason: str) -> None:
        with self._connection:
            self._connection.execute(
                """UPDATE session_permission_state SET failure = ?, updated_at = ?
                   WHERE session_id = ?""",
                (reason, self._stamp(), str(session_id)),
            )

    async def set_requested(self, session_id: SessionId, preset: PermissionPreset) -> None:
        with self._connection:
            changed = self._connection.execute(
                """UPDATE session_permission_state
                   SET requested = ?, failure = NULL, updated_at = ?
                   WHERE session_id = ?""",
                (PermissionPreset(preset).value, self._stamp(), str(session_id)),
            ).rowcount
        if not changed:
            raise LookupError("session has no permission state")

    def _stamp(self) -> str:
        return self._clock().astimezone(UTC).isoformat()
