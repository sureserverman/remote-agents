"""Launch permission snapshots and their apply state, outside positional session projections."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from remote_agents.domain.models import SessionId
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
    ) -> bool:
        # An UPDATE, never an upsert: a session with no row was launched by an older build.
        # Checked and written in one statement, as `mark_failed` is.
        preset = PermissionPreset(preset)
        with self._connection:
            changed = self._connection.execute(
                """UPDATE session_permission_state
                   SET applied = ?, daemon = ?, failure = NULL, updated_at = ?
                   WHERE session_id = ? AND requested = ?""",
                (preset.value, daemon, self._stamp(), str(session_id), preset.value),
            ).rowcount
        return bool(changed)

    async def mark_failed(
        self, session_id: SessionId, reason: str, *, requested: PermissionPreset
    ) -> bool:
        # One statement checks the request and writes the failure: an apply runs for seconds,
        # and a preset asked for meanwhile must not inherit the old one's failure.
        with self._connection:
            changed = self._connection.execute(
                """UPDATE session_permission_state
                   SET failure = ?, applied = NULL, daemon = NULL, updated_at = ?
                   WHERE session_id = ? AND requested = ?""",
                (reason, self._stamp(), str(session_id), PermissionPreset(requested).value),
            ).rowcount
        return bool(changed)

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

    async def restore_requested(
        self, session_id: SessionId, preset: PermissionPreset, *, replacing: PermissionPreset
    ) -> bool:
        # One statement checks and writes, as `mark_failed` does: another surface's change made
        # meanwhile is never overwritten by this one's undo.
        with self._connection:
            changed = self._connection.execute(
                """UPDATE session_permission_state
                   SET requested = ?, applied = NULL, daemon = NULL, failure = NULL,
                       updated_at = ?
                   WHERE session_id = ? AND requested = ?""",
                (
                    PermissionPreset(preset).value,
                    self._stamp(),
                    str(session_id),
                    PermissionPreset(replacing).value,
                ),
            ).rowcount
        return bool(changed)

    def _stamp(self) -> str:
        return self._clock().astimezone(UTC).isoformat()
