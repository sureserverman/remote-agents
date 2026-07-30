"""Parameterized SQLite persistence for session projections and append-only events."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Sequence
from datetime import UTC, datetime

from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.state_machine import LifecycleEvent, transition


class SQLiteSessionStore:
    """Store durable metadata only; terminal pane content never enters this adapter."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    async def next_sequence(self, project_id: ProjectId, profile_id: ProfileId) -> int:
        """Allocate the next persisted display sequence for one project/profile pair."""
        result = self._connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE project_id = ? AND profile_id = ?",
            (str(project_id), str(profile_id)),
        ).fetchone()
        return int(result[0]) + 1

    async def save(self, record: SessionRecord) -> None:
        """Insert a session projection using bound values rather than SQL interpolation."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO sessions(
                    session_id, project_id, profile_id, display_identity, state, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.session_id),
                    str(record.project_id),
                    str(record.profile_id),
                    record.display.rendered,
                    record.state.value,
                    record.created_at.astimezone(UTC).isoformat(),
                ),
            )

    async def get(self, session_id: SessionId) -> SessionRecord | None:
        """Load one immutable projection by its opaque session identifier."""
        row = self._connection.execute(
            """
            SELECT session_id, project_id, profile_id, display_identity, state, created_at
            FROM sessions WHERE session_id = ?
            """,
            (str(session_id),),
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    async def list(self, states: Collection[SessionState] | None = None) -> Sequence[SessionRecord]:
        """Return durable projections, optionally filtered by approved lifecycle states."""
        query = (
            "SELECT session_id, project_id, profile_id, display_identity, state, created_at "
            "FROM sessions"
        )
        values: tuple[str, ...] = ()
        if states:
            values = tuple(state.value for state in states)
            query += f" WHERE state IN ({', '.join('?' for _ in values)})"
        rows = self._connection.execute(query, values).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    async def record_event(self, session_id: SessionId, event: LifecycleEvent) -> SessionRecord:
        """Append a lifecycle event and atomically refresh its session projection."""
        current = await self.get(session_id)
        if current is None:
            raise KeyError(f"unknown session: {session_id}")
        updated = SessionRecord(
            current.session_id,
            current.project_id,
            current.profile_id,
            current.display,
            transition(current.state, event).to_state,
            current.created_at,
        )
        with self._connection:
            self._append_event(str(session_id), event)
            self._connection.execute(
                "UPDATE sessions SET state = ? WHERE session_id = ?",
                (updated.state.value, str(session_id)),
            )
        return updated

    def append_event(
        self,
        session_id: str,
        event: LifecycleEvent,
        *,
        idempotency_key: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Append a sanitized lifecycle event without pane, prompt, token, or environment data."""
        with self._connection:
            self._append_event(
                session_id,
                event,
                idempotency_key=idempotency_key,
                error_code=error_code,
            )

    def _append_event(
        self,
        session_id: str,
        event: LifecycleEvent,
        *,
        idempotency_key: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Write a validated event within the caller's transaction boundary."""
        if error_code and any(
            fragment in error_code.casefold() for fragment in ("token", "prompt", "pane", "env")
        ):
            raise ValueError("event error code must be sanitized")
        self._connection.execute(
            """
            INSERT INTO session_events(
                session_id, event_type, created_at, idempotency_key, error_code
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_id,
                event.value,
                datetime.now(UTC).isoformat(),
                idempotency_key,
                error_code,
            ),
        )

    async def claim_idempotency_key(self, key: str) -> bool:
        """Atomically claim a callback key without creating a fake session event."""
        with self._connection:
            try:
                self._connection.execute(
                    "INSERT INTO idempotency_claims(key, created_at) VALUES (?, ?)",
                    (key, datetime.now(UTC).isoformat()),
                )
            except sqlite3.IntegrityError:
                return False
        return True


def _record_from_row(row: tuple[str, str, str, str, str, str]) -> SessionRecord:
    """Rebuild a validated domain record from one trusted SQLite projection row."""
    display_parts = row[3].split(" · ", 4)
    if len(display_parts) not in {4, 5} or not display_parts[3].startswith("#"):
        raise ValueError("stored display identity is invalid")
    display = SessionDisplayIdentity(
        display_parts[0],
        display_parts[1],
        display_parts[2],
        int(display_parts[3][1:]),
        display_parts[4] if len(display_parts) == 5 else None,
    )
    return SessionRecord(
        SessionId.parse(row[0]),
        ProjectId(row[1]),
        ProfileId(row[2]),
        display,
        SessionState(row[4]),
        datetime.fromisoformat(row[5]),
    )
