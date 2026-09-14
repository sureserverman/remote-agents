"""Move the surface's own bookkeeping out of the watched store, once, without losing a row.

**Why the move exists.** `StoreWatch` fingerprints a file. While the bot's callback tokens
shared the domain store's bytes, minting a keyboard and launching a session were the same
event to the watcher — so an open sessions page redrew, minted, republished its own change and
redrew again, thirty real edits a minute into a chat Telegram then flood-banned for six hours.
The signal cannot mean "a session changed" while the file also carries what the surface writes
about itself.

**Why this is a function and not migration 14 alone.** Migration 14 drops the moved tables from
the domain store, and `open_database` applies pending migrations the moment it opens. A run
that opened the domain store first would drop the rows before anything copied them — the
migration would report success and the rows would be gone. So the copy runs here, on a raw
connection that applies no migrations, and must be called *before* the domain store is opened
normally. Nothing here yet holds that order automatically: the test that will,
`test_the_rows_survive_a_domain_open_that_applies_the_drop`, arrives in Stage 2 with migration
14, because an ordering between a copy and a drop cannot be asserted while the drop does not
exist. An earlier version of this docstring named that test in the present tense, which is the
worse half of the same failure — a reader trusting it would not go looking for the gap. What is
assertable now is that this function applies no migrations at all, and
`test_the_split_applies_no_migration_to_the_domain_store` does that.

**Why `INSERT OR IGNORE` rather than a plain insert.** A crash between the copy and the drop
leaves the rows in both files, which is the safe direction. The next run finds the tables still
present and copies again, and the ignore makes that second copy a no-op instead of a primary
key collision.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from remote_agents.adapters.sqlite.database import open_ui_database, ui_database_path
from remote_agents.adapters.sqlite.migrations import UI_TABLES

__all__ = ["SplitReport", "split_stores"]


@dataclass(frozen=True)
class SplitReport:
    """What the move did, named per table rather than totalled.

    A total cannot say *which* table came up short, and the gate's verifier compares the set.
    """

    moved: dict[str, int]
    backup: Path | None


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        name
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _backup(connection: sqlite3.Connection, domain_path: Path) -> Path:
    """A timestamped snapshot taken before a single row moves.

    Deliberately not `backup_path`'s single `.bak`: that one is `open_database`'s to overwrite
    on any migration, and a rollback wants the copy taken at *this* moment, still there after
    the next start has written its own.
    """
    # PID and a short random tail, not the timestamp alone. `strftime` resolves to the second
    # and this project runs up to five writers across four processes: two starting inside the
    # same second computed the same path and raced on it, which failed a healthy startup.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    unique = f"{stamp}-{os.getpid()}-{secrets.token_hex(3)}"
    destination = domain_path.with_name(f"{domain_path.name}.pre-split-{unique}.bak")
    with sqlite3.connect(destination) as target:
        connection.backup(target)
    return destination


def _already_copied(connection: sqlite3.Connection, domain_path: Path) -> bool:
    """Whether the UI store already holds at least what the domain store has, table for table."""
    ui_path = ui_database_path(domain_path)
    if not ui_path.exists():
        return False
    ui = sqlite3.connect(f"file:{ui_path}?mode=ro", uri=True)
    try:
        landed = _tables(ui)
        for table in _tables(connection) & set(UI_TABLES):
            if table not in landed:
                return False
            here = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            there = ui.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if there < here:
                return False
        return True
    finally:
        ui.close()


def split_stores(domain_path: Path) -> SplitReport:
    """Copy every moved table into the UI store. Idempotent; leaves the drop to migration 14.

    Answers an empty report when there is nothing to do — a store already split, or one that
    does not exist yet — because this runs on every process start.
    """
    if not domain_path.exists():
        return SplitReport(moved={}, backup=None)

    # Raw, not `open_database`: opening normally would apply migration 14 and drop the very
    # tables this function exists to read.
    connection = sqlite3.connect(domain_path)
    try:
        present = _tables(connection) & set(UI_TABLES)
        if not present:
            return SplitReport(moved={}, backup=None)

        # Already landed? Then this start has nothing to do, and must not take another backup.
        # Stage 1 is additive, so `present` stays non-empty until Stage 2's drop -- without this
        # every process start for the whole of that window wrote a fresh full-database copy and
        # nothing ever removed them. Checked against the UI store's contents rather than a flag,
        # because a flag is a second opinion about a question the rows already answer.
        if _already_copied(connection, domain_path):
            return SplitReport(moved={}, backup=None)

        backup = _backup(connection, domain_path)
        ui_path = ui_database_path(domain_path)
        open_ui_database(ui_path).close()

        connection.execute("ATTACH DATABASE ? AS ui", (str(ui_path),))
        try:
            moved: dict[str, int] = {}
            for table in UI_TABLES:
                if table not in present:
                    continue
                connection.execute(
                    f'INSERT OR IGNORE INTO ui."{table}" SELECT * FROM main."{table}"'
                )
                moved[table] = connection.execute(
                    f'SELECT COUNT(*) FROM main."{table}"'
                ).fetchone()[0]
            connection.commit()

            # Verified before the drop is ever allowed to run, and per table: a short copy
            # discovered after migration 14 has run is data nobody can get back without the
            # backup taken above.
            for table, expected in moved.items():
                landed = connection.execute(f'SELECT COUNT(*) FROM ui."{table}"').fetchone()[0]
                if landed < expected:
                    raise RuntimeError(
                        f"{table}: {expected} rows in the domain store, {landed} landed in "
                        f"the UI store; refusing to go further. The pre-split backup is "
                        f"{backup}"
                    )
        finally:
            connection.execute("DETACH DATABASE ui")
        return SplitReport(moved=moved, backup=backup)
    finally:
        connection.close()
