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
    # Callback tokens carry no expiry column: a token is valid for as long as the message it
    # was drawn on, so (chat_id, message_id) scopes validity instead of a clock, and retention
    # is bounded by message life and by size rather than by a timed sweep.
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
    # No DEFAULT, deliberately: every row that predates this column reads NULL and so takes
    # the conservative branch. Provenance cannot be back-derived (DEC-020) -- once a pane has
    # been adopted a record exists, so the next reconciliation pass matches it by id and never
    # sees an unknown pane again -- and backfilling 'adopted' would hand a destructive action
    # to rows on the strength of a guess about how they got there.
    (
        6,
        "ALTER TABLE sessions ADD COLUMN orphan_provenance TEXT;",
    ),
    # Nullable, and deliberately not backfilled -- for DEC-020's reason one column over. An
    # unmigrated row and a session nobody has toggled are both genuinely *unknown*, and the
    # surfaces treat unknown by offering both Remote Control actions, which is exactly what
    # they did before this column existed. Backfilling either value would hand a surface an
    # action to hide on the strength of a guess about a pane it never observed.
    (
        7,
        "ALTER TABLE sessions ADD COLUMN remote_control_state TEXT;",
    ),
    # A conversation's resume identity is exclusive while its session can still be live, and
    # released once it cannot. Before this, the index held for the row's whole existence and
    # nothing deletes a session row -- so one resume bound a conversation to one session
    # forever, and after that session ended the tool could never resume it again. Removing
    # the bot's resume confirmation is what made a mis-tap enough to trigger it, which is how
    # it was found.
    #
    # `state <> 'ended'` rather than a list, because ENDED is exactly
    # `state_machine.TERMINAL_STATES` -- the states the transition matrix offers no way out
    # of. A row leaves the index when it reaches ENDED and the next resume may bind, while
    # two live panes on one conversation stay impossible, which is what the index was for.
    #
    # The ended row keeps its `resume_profile_id`/`resume_source_id`: history is what answers
    # *what was resumed*, and a partial index releases exclusivity without erasing it.
    (
        8,
        """
        DROP INDEX IF EXISTS sessions_resume_identity;
        CREATE UNIQUE INDEX sessions_resume_identity
        ON sessions(resume_profile_id, resume_source_id)
        WHERE resume_profile_id IS NOT NULL
          AND resume_source_id IS NOT NULL
          AND state <> 'ended';
        """,
    ),
    # Observations, never delivery state: the Telegram notifier's queue, rate windows, and
    # standing messages stay in memory by decision (DEC-026) — this table is the local
    # feed's durable source. Append-only; INTEGER PRIMARY KEY is the read order, because
    # insertion order is the one clock every writer shares.
    (
        9,
        """
        CREATE TABLE agent_activity (
            activity_id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            detail TEXT,
            confidence TEXT NOT NULL,
            observed_at TEXT NOT NULL
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
