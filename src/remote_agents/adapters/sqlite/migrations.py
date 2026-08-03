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
        """
        CREATE TABLE handoff_intents (
            intent_id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            conversation_source_id TEXT NOT NULL,
            process_pid INTEGER NOT NULL,
            process_start_ticks INTEGER NOT NULL,
            process_euid INTEGER NOT NULL,
            process_name TEXT NOT NULL,
            state TEXT NOT NULL
        );
        CREATE UNIQUE INDEX handoff_intents_source
        ON handoff_intents(profile_id, conversation_source_id)
        WHERE state IN ('requested', 'stop_sent');
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
