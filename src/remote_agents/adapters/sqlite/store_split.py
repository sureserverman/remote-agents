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
normally. `test_the_rows_survive_a_domain_open_that_applies_the_drop` holds that order,
together with
`test_the_split_applies_no_migration_to_the_domain_store`, which pins the mechanism the
ordering rests on. Both exist and both fail on the reverted behaviour — an earlier version of
this docstring named the first of them before it existed, which is why the claim is written
against tests that are checked rather than remembered.

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
from remote_agents.adapters.sqlite.migrations import MOVED_TABLES
from remote_agents.ports.private_directory import open_private_directory

__all__ = ["RestoreReport", "SplitReport", "split_stores", "unsplit_stores"]


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
    target = sqlite3.connect(destination)
    try:
        connection.backup(target)
    finally:
        target.close()
    # Owner-only, for the same reason `open_ui_database` narrows the UI store: this is a full
    # copy of the domain database *including* the callback tokens, the owner's user id and their
    # chat id. The 0700 state directory contains it either way; leaving the one file this diff
    # argued deserves 0600 at the process umask would be an odd place to stop.
    os.chmod(destination, 0o600)
    return destination


def _already_copied(connection: sqlite3.Connection, domain_path: Path) -> bool:
    """Whether the UI store already holds at least what the domain store has, table for table."""
    ui_path = ui_database_path(domain_path)
    if not ui_path.exists():
        return False
    ui = sqlite3.connect(f"file:{ui_path}?mode=ro", uri=True)
    try:
        landed = _tables(ui)
        for table in _tables(connection) & set(MOVED_TABLES):
            if table not in landed:
                return False
            try:
                here = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            except sqlite3.OperationalError:
                # The table went out from under us: another process split and let migration 14
                # drop it between our listing and this count. Its copy is verified before its
                # drop, so the rows are safe and there is nothing left for us to do. Answering
                # True is the truth — this store IS already copied — where raising would crash
                # a `serve` and an operator's `tui` that merely started in the same second.
                return True
            there = ui.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if there < here:
                return False
        return True
    finally:
        ui.close()


def split_stores(domain_path: Path) -> SplitReport:
    """Copy every moved table into the UI store. Idempotent; leaves the drop to migration 14.

    Answers an empty report when there is nothing to do — a store already split, or one that
    does not exist yet — because it is meant to run on every process start. `bootstrap`
    calls it before
    opening the domain store, which is the order migration 14 makes load-bearing.
    """
    if not domain_path.exists():
        return SplitReport(moved={}, backup=None)

    # The parent is vetted here rather than relied on from the caller: `_open_domain_store` runs
    # this *before* `paths.open_database` applies its own guard, and one caller
    # (`_print_session_history`) does not vet the directory beforehand at all — so without this,
    # that path would read the store and write a full backup of it through an unvetted parent.
    # Note the side effect: `open_private_directory` re-applies 0700 to the parent even when
    # it already holds, and this runs on every process start. Harmless under `ProductionPaths`,
    # which keeps it at 0700 anyway, but it is a write and worth not being surprised by.
    if open_private_directory(domain_path.parent) is None:
        raise ValueError("database directory cannot traverse a symlink")
    # Raw, not `open_database`: opening normally would apply migration 14 and drop the very
    # tables this function exists to read.
    connection = sqlite3.connect(domain_path)
    try:
        present = _tables(connection) & set(MOVED_TABLES)
        if not present:
            return SplitReport(moved={}, backup=None)

        # Already landed? Then this start has nothing to do and must not back up again.
        # Stage 1 is additive, so `present` stays non-empty until Stage 2's drop -- without this
        # every process start, once this is wired, would otherwise write a fresh full copy and
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
            for table in MOVED_TABLES:
                if table not in present:
                    continue
                try:
                    cursor = connection.execute(
                        f'INSERT OR IGNORE INTO ui."{table}" SELECT * FROM main."{table}"'
                    )
                except sqlite3.OperationalError:
                    # Same race, one step later: a concurrent split dropped this table after we
                    # listed it. Skipped rather than fatal, for the same reason.
                    continue
                # What THIS call copied, not the table's total. Counting the source gave a
                # number identical whether the copy did everything or nothing, and both
                # reports are read as proof that it worked.
                moved[table] = cursor.rowcount if cursor.rowcount > 0 else 0
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


@dataclass(frozen=True)
class RestoreReport:
    """What the rollback put back, named per table for the same reason `SplitReport` is."""

    restored: dict[str, int]


#: Every `CREATE` form `sqlite_master` can hand back for a table this restore recreates.
#: Longest first, so `CREATE UNIQUE INDEX` is matched before `CREATE INDEX` would shadow it.
_CREATE_FORMS = ("CREATE UNIQUE INDEX", "CREATE TABLE", "CREATE INDEX", "CREATE TRIGGER")


def _idempotent(sql: str) -> str:
    """The same statement, safe to run against a store that already has the object.

    An operator under pressure runs a restore twice, so every statement it issues has to be a
    no-op the second time.
    """
    for form in _CREATE_FORMS:
        if sql.startswith(form):
            return sql.replace(form, f"{form} IF NOT EXISTS", 1)
    raise RuntimeError(f"cannot make this statement idempotent: {sql[:60]!r}")


def _refuse_drifted_table(connection: sqlite3.Connection, table: str) -> None:
    """Refuse a domain table whose columns no longer match the UI store's.

    `CREATE TABLE IF NOT EXISTS` is a no-op against a table that already exists with a different
    shape -- left over from an older partial restore, say. The `INSERT ... SELECT *` that follows
    matches columns by POSITION, so a drifted table either raises something obscure or, when the
    counts happen to line up, writes each value into the wrong column. Saying so is the only
    honest option: this function restores a schema, it does not reconcile two.
    """
    here = [row[1] for row in connection.execute(f'PRAGMA main.table_info("{table}")')]
    there = [row[1] for row in connection.execute(f'PRAGMA ui.table_info("{table}")')]
    if here != there:
        raise RuntimeError(
            f"{table}: the domain store's columns {here} do not match the UI store's {there}; "
            "refusing to copy positionally into a different shape"
        )


def unsplit_stores(domain_path: Path) -> RestoreReport:
    """Put the surface tables back in the domain store. The operator's way out.

    **Written to work after the drop, not only before it**, because before it there is nothing
    to undo — the domain store still has everything and rolling back is deleting one file. It is
    once Stage 2 has dropped the tables that an operator needs this, and by then putting the rows
    back means recreating the tables too.

    **The schema comes from the UI store's own `sqlite_master`, not from a copy of the DDL
    here.** A second spelling of those tables would be a second opinion about their shape
    (DEC-011), and the one that drifted would be this one — it is the path nobody exercises
    until the day it matters.

    Idempotent, because an operator who is not sure a command worked runs it again.

    **Raises rather than guessing**, in two cases the runbook names: a domain table whose
    columns have drifted from the UI store's, and a schema object this does not know how to
    recreate. Both are plausible on exactly the messy store this function exists for, so they
    fail with a sentence rather than landing values in the wrong columns.
    """
    ui_path = ui_database_path(domain_path)
    if not domain_path.exists() or not ui_path.exists():
        return RestoreReport(restored={})

    connection = sqlite3.connect(domain_path)
    try:
        connection.execute("ATTACH DATABASE ? AS ui", (str(ui_path),))
        try:
            restored: dict[str, int] = {}
            for table in MOVED_TABLES:
                schema = connection.execute(
                    "SELECT type, sql FROM ui.sqlite_master "
                    "WHERE tbl_name = ? AND sql IS NOT NULL",
                    (table,),
                ).fetchall()
                if not any(kind == "table" for kind, _ in schema):
                    continue
                unhandled = {kind for kind, _ in schema} - {"table", "index", "trigger"}
                if unhandled:
                    # Loudly, rather than skipping it. The first version filtered the execution
                    # loop to tables and indexes, so a trigger was fetched and then silently
                    # discarded -- a restore quietly less faithful than the docstring claimed.
                    raise RuntimeError(
                        f"{table}: {sorted(unhandled)} in the UI store's schema is not something "
                        "this restore knows how to recreate"
                    )
                # Tables first, then indexes and triggers: neither can be created before the
                # table it is on, and `sqlite_master` does not promise that order.
                for kind, sql in sorted(schema, key=lambda row: row[0] != "table"):
                    connection.execute(_idempotent(sql))
                _refuse_drifted_table(connection, table)
                cursor = connection.execute(
                    f'INSERT OR IGNORE INTO main."{table}" SELECT * FROM ui."{table}"'
                )
                # Rows this call actually put back. Reporting the domain table's total instead
                # printed the same figure whether the rollback restored everything or nothing,
                # while the runbook told the operator to read it as proof.
                restored[table] = cursor.rowcount if cursor.rowcount > 0 else 0
            connection.commit()
        except Exception:
            # Rolled back BEFORE the detach. `DETACH DATABASE` raises "database ui is locked"
            # while an uncommitted transaction still touches the attached file, so without this
            # the failure an operator sees is the detach's, and the real cause is gone. Verified
            # as a live failure mode by the review that asked for this.
            connection.rollback()
            raise
        finally:
            connection.execute("DETACH DATABASE ui")
        return RestoreReport(restored=restored)
    finally:
        connection.close()
