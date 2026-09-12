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
    # Observations, never delivery state: the Telegram notifier's queue and rate windows stay
    # in memory by decision (DEC-026) — this table is the local feed's durable source. Its
    # *standing messages* were on that list and are not any more; migration 10 gives them a
    # table, for a reason DEC-026 did not weigh and this comment must not keep denying.
    # Append-only; INTEGER PRIMARY KEY is the read order, because
    # insertion order is the one clock every writer shares.
    #
    # **Invariant: rows are never deleted.** Without AUTOINCREMENT, sqlite reuses the max
    # rowid after a delete, and a reused id would silently misorder the newest-first read.
    # A future retention sweep must therefore not land against this schema — revisit this
    # migration (a table rebuild to AUTOINCREMENT) before any delete path ships. Stated
    # here rather than fixed with AUTOINCREMENT because v9 is already applied on a live
    # database, and editing an applied migration's SQL forks the schema history.
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
    # Which message a session's notification *is*, so a restart amends the one in the chat
    # instead of sending a second beside it. Not the durable queue DEC-026 declined: nothing
    # here is drained, and a row is not news waiting to be delivered — it names a message the
    # owner is already looking at. The queue and the rate windows are still in memory.
    #
    # Keyed on (chat_id, session_id) because "one notification per session" is the invariant
    # being stored, and a primary key is the only place to state it that a second writer
    # cannot get between. Rows are deleted, unlike `agent_activity`: one goes when the owner
    # presses the message's button, and one goes when the session stops being notifiable —
    # which is what makes a finished session's alert leave the chat rather than sit there
    # offering to open something that has ended.
    #
    # `activities` is the rendered story as JSON, so an amendment after a restart still says
    # "finished, then asked a question" rather than only the newest line. Denormalised on
    # purpose: `agent_activity` is the feed's append-only record of what was *observed*, and
    # joining the two would make the message's text depend on a retention sweep that table's
    # own invariant contemplates.
    (
        10,
        """
        CREATE TABLE standing_notifications (
            chat_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            token TEXT NOT NULL,
            activities TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, session_id)
        );
        """,
    ),
    (
        11,
        """
        ALTER TABLE agent_activity ADD COLUMN ask TEXT;
        """,
    ),
    # One standing trust question per session, so a restart does not ask it twice.
    #
    # **Keyed on the session alone, unlike `standing_notifications` above**, and the difference
    # is what the two messages are about. An activity notification is about a chat's view of a
    # session, so the same session can legitimately stand in more than one; the trust question
    # is about the *session*, which has exactly one answer whoever is looking. A second row
    # would be a second copy of a question that can only be answered once.
    #
    # `settled` rather than deleting the row: the amendment (DEC-034) edits the message in
    # place, so the row has to outlive the answer in order to name the message being amended.
    # A deleted row and an unsettled one are the same absence, and the pass would send again.
    (
        12,
        """
        CREATE TABLE trust_notifications (
            session_id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            settled INTEGER NOT NULL DEFAULT 0
        );
        """,
    ),
    # The retired `claude-remote` profile becomes the `claude` it always was.
    #
    # The two ids named the same executable -- `claude` and `claude --remote-control
    # {managed_name}` -- so this renames a launch flag that had been modelled as a second
    # agent. Stage 4 makes the flag a property of the launch, which leaves every stored row
    # pointing at a profile id nothing curates any more: `closed_profiles()` would not return
    # it, so a surface rendering one of these rows has no label for it and `ProfileUsageReaders`
    # has no reader.
    #
    # **Both columns, and only those two.** `profile_id` is what the session was launched as;
    # `resume_profile_id` is what a *resumed* session's provider conversation belongs to, and a
    # rewrite of one is invisible to a reader of the other. Every other column is left exactly
    # as it stands -- `display_identity` included, deliberately: it carries the agent label the
    # session was created under, which is history and not this migration's to edit. A session
    # whose list row still reads `claude-remote` is telling the truth about what started it.
    #
    # The WHERE clauses are what keep it to those rows. An UPDATE without them would rewrite
    # every session's profile to `claude`, turning codex sessions into Claude ones, and the
    # profile columns alone cannot show that -- which is why the test compares every column of
    # every row before and after.
    #
    # **The first statement exists because the rename can violate a unique index**, and the
    # first form of this migration did. `sessions_resume_identity` is UNIQUE on
    # `(resume_profile_id, resume_source_id)` for every non-ended row. While both ids existed,
    # resuming one Claude conversation under each produced `('claude', X)` and
    # `('claude-remote', X)` -- two distinct keys, accepted by the index, and not serialised
    # against each other because `SessionService._resume_locked` locks per *(profile,
    # conversation)*. Renaming makes both `('claude', X)`: `IntegrityError`, the whole
    # migration rolled back, the schema left at 12. `open_database` migrates on every start,
    # so that host's service would fail to start and keep failing.
    #
    # The tie-break keeps both sessions. The row under the **retired** id gives up its resume
    # *binding* -- not its row, not its state -- so the id that still exists keeps the
    # conversation and nothing is deleted. It is also what the index has always meant (two
    # live panes on one conversation are impossible), applied to data the old model admitted
    # only because it counted the two ids as different agents.
    #
    # Scoped to non-ended rows on both sides, because ended rows are outside the partial index
    # and so cannot collide -- they keep the conversation they record having resumed.
    #
    # Safe to re-run, safe on a database that never held the retired id, and safe to arrive
    # late: every statement is a no-op once applied, and `open_database` takes its
    # pre-migration backup before any of it runs (`docs/database-recovery.md`).
    (
        13,
        """
        UPDATE sessions SET resume_profile_id = NULL, resume_source_id = NULL
        WHERE resume_profile_id = 'claude-remote'
          AND state <> 'ended'
          AND resume_source_id IN (
              SELECT resume_source_id FROM sessions
              WHERE resume_profile_id = 'claude' AND state <> 'ended'
          );
        UPDATE sessions SET profile_id = 'claude' WHERE profile_id = 'claude-remote';
        UPDATE sessions SET resume_profile_id = 'claude' WHERE resume_profile_id = 'claude-remote';
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
