"""Parameterized SQLite persistence for session projections and append-only events."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from remote_agents.domain.models import SessionRecord
from remote_agents.domain.state_machine import LifecycleEvent


class SQLiteSessionStore:
    """Store durable metadata only; terminal pane content never enters this adapter."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def save(self, record: SessionRecord) -> None:
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

    def append_event(
        self,
        session_id: str,
        event: LifecycleEvent,
        *,
        idempotency_key: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Append a sanitized lifecycle event without pane, prompt, token, or environment data."""
        if error_code and any(
            fragment in error_code.casefold() for fragment in ("token", "prompt", "pane", "env")
        ):
            raise ValueError("event error code must be sanitized")
        with self._connection:
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

    def claim_idempotency_key(self, key: str) -> bool:
        """Atomically claim a key; duplicates return false without side effects."""
        with self._connection:
            try:
                self._connection.execute(
                    """
                    INSERT INTO session_events(session_id, event_type, created_at, idempotency_key)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        "00000000-0000-0000-0000-000000000000",
                        "idempotency_claim",
                        datetime.now(UTC).isoformat(),
                        key,
                    ),
                )
            except sqlite3.IntegrityError:
                return False
        return True
