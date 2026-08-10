"""Monotonic SQLite schema migrations for safe local metadata."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            display_identity TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            terminal_reason TEXT
        );
        CREATE TABLE session_events (
            event_id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(session_id),
            event_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            idempotency_key TEXT UNIQUE,
            error_code TEXT
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE idempotency_claims (
            key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        );
        """,
    ),
    (
        3,
        """
        ALTER TABLE sessions ADD COLUMN resume_profile_id TEXT;
        ALTER TABLE sessions ADD COLUMN resume_source_id TEXT;
        CREATE UNIQUE INDEX sessions_resume_identity
        ON sessions(resume_profile_id, resume_source_id)
        WHERE resume_profile_id IS NOT NULL AND resume_source_id IS NOT NULL;
        """,
    ),
    (
        4,
        "",
    ),
    # Callback tokens carry no expires_at: a token is valid for as long as the message it was
    # drawn on, so (chat_id, message_id) scopes validity instead of a clock, and retention is
    # bounded by message life and by size rather than by a TTL sweep.
    (
        5,
        """
        CREATE TABLE callback_states (
            token TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            mutation INTEGER NOT NULL,
            claimed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX callback_states_message ON callback_states(chat_id, message_id);
        CREATE TABLE chat_views (
            chat_id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
)


def current_version(connection: sqlite3.Connection) -> int:
    """Return zero for an uninitialized database or its recorded schema version."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if not exists:
        return 0
    return int(connection.execute("SELECT version FROM schema_version").fetchone()[0])


def apply_migrations(
    connection: sqlite3.Connection, migrations: Iterable[tuple[int, str]] = MIGRATIONS
) -> None:
    """Apply each next migration atomically and record only monotonic versions."""
    connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    if connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
        connection.execute("INSERT INTO schema_version(version) VALUES (0)")
    connection.commit()
    version = current_version(connection)
    for target, sql in migrations:
        if target <= version:
            continue
        if target != version + 1:
            raise ValueError("migrations must be contiguous and monotonic")
        try:
            connection.execute("BEGIN")
            for statement in (part.strip() for part in sql.split(";") if part.strip()):
                connection.execute(statement)
            connection.execute("UPDATE schema_version SET version = ?", (target,))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        version = target
