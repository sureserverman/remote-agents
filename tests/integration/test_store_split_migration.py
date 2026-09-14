"""Moving the surface's bookkeeping out of the domain store, without losing a row.

**Stage 1 is additive on purpose.** `split_stores` copies the surface tables into their own
store and removes nothing: the domain store still carries them until Stage 2 moves the readers
and migration 14 takes them out, in one change. Dropping them here broke 44 tests between the
two stages — every surface store still reading the domain connection — which is the half-built
state a stage gate exists to refuse. It also makes Stage 1 reversible by deleting one file.

`split_stores` still copies on a raw connection that applies no migrations, because once
migration 14 exists, opening the domain store normally would drop the very tables it reads.

These tests build a store through `open_database` and fill every moved table, rather than
reading the operator's live one: the schema is identical because it comes from the same
migration list, and a test that needs a particular machine's database is not a test. The gate
runs the same move against a real copy, which is where real row counts are proved.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite import database
from remote_agents.adapters.sqlite.database import (
    open_database,
    open_ui_database,
    ui_database_path,
)
from remote_agents.adapters.sqlite.migrations import MIGRATIONS, UI_TABLES
from remote_agents.adapters.sqlite.store_split import split_stores

#: Everything up to but not including the drop, which Stage 2 adds as migration 14. Opening at
#: this list is what a store predating the split looks like, and it is the only thing
#: `split_stores` is ever asked to operate on.
_PRE_SPLIT = tuple(entry for entry in MIGRATIONS if entry[0] < 14)

_ROWS = {
    "callback_states": [
        ("c1_token_a", "session.detail", "e1", 7, 11, 100, 0, 0, "2026-09-14T00:00:00+00:00"),
        ("c1_token_b", "session.stop", "e2", 7, 11, 100, 1, 0, "2026-09-14T00:00:01+00:00"),
    ],
    "chat_views": [(11, 1669, "2026-09-14T00:00:00+00:00")],
    "standing_notifications": [(11, "s1", 900, "tok", "[]", "2026-09-14T00:00:00+00:00")],
    "trust_notifications": [("s1", 11, 901, 0)],
}


def _a_store_with_rows(path: Path) -> dict[str, int]:
    """A domain store at the pre-split schema, with something in every moved table."""
    connection = open_database(path, migrations=_PRE_SPLIT)
    try:
        for table, rows in _ROWS.items():
            placeholders = ", ".join("?" * len(rows[0]))
            connection.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', rows)
        connection.execute(
            "INSERT INTO sessions(session_id, project_id, profile_id, display_identity, "
            "state, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("s1", "p1", "claude", "Demo", "running", "2026-09-14T00:00:00+00:00"),
        )
        connection.commit()
        return {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in UI_TABLES
        }
    finally:
        connection.close()


def _counts(path: Path, tables: tuple[str, ...]) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        present = {
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        return {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables
            if table in present
        }
    finally:
        connection.close()


def test_the_migration_moves_every_row_of_every_moved_table(tmp_path: Path) -> None:
    """Swept over `UI_TABLES`. A spot check on one table cannot fail on the other five."""
    domain = tmp_path / "sessions.sqlite3"
    before = _a_store_with_rows(domain)
    assert all(before.values()), "a table with no rows proves nothing about moving rows"

    split_stores(domain)

    after = _counts(ui_database_path(domain), UI_TABLES)
    assert after == before


def test_the_domain_store_keeps_its_own_rows(tmp_path: Path) -> None:
    """The obvious way to pass every other test in this file is to lose everything."""
    domain = tmp_path / "sessions.sqlite3"
    _a_store_with_rows(domain)

    split_stores(domain)
    open_database(domain).close()

    assert _counts(domain, ("sessions",)) == {"sessions": 1}


def test_the_migration_is_a_no_op_the_second_time(tmp_path: Path) -> None:
    """It runs on every process start, so it has to be safe to run when there is nothing to do."""
    domain = tmp_path / "sessions.sqlite3"
    before = _a_store_with_rows(domain)

    split_stores(domain)
    open_database(domain).close()
    split_stores(domain)
    split_stores(domain)

    assert _counts(ui_database_path(domain), UI_TABLES) == before


def test_the_migration_leaves_a_backup_carrying_the_rows_the_drop_will_remove(
    tmp_path: Path,
) -> None:
    """The rollback restores this file; without it there is nothing to restore.

    **Named for what it can actually falsify.** It was
    `..._writes_a_backup_before_it_moves_anything`, and that name claimed an ordering this test
    cannot see: `split_stores` only copies, and the drop is migration 14 afterwards, so a
    backup taken before the copy and one taken after it hold identical bytes. Moving the
    `_backup` call after the copy left every check here green — a survived mutation that was
    right to survive, because the distinction is one the code does not make. What is real, and
    what this asserts, is that a backup exists carrying the rows before anything can drop them.
    """
    domain = tmp_path / "sessions.sqlite3"
    _a_store_with_rows(domain)

    report = split_stores(domain)

    assert report.backup is not None
    assert report.backup.exists()
    assert _counts(report.backup, UI_TABLES), "the backup predates the move, so it has the rows"


def test_a_store_that_was_never_split_and_has_no_moved_tables_is_left_alone(
    tmp_path: Path,
) -> None:
    """A UI store opened on its own must not be mistaken for a domain store needing a move."""
    ui_only = tmp_path / "ui.sqlite3"
    open_ui_database(ui_only).close()

    report = split_stores(tmp_path / "sessions.sqlite3")

    assert report.moved == {}


@pytest.mark.parametrize("table", sorted(UI_TABLES))
def test_every_moved_table_is_reported_by_name(tmp_path: Path, table: str) -> None:
    """The report is what the gate's verifier reads, so it names the set rather than a total."""
    domain = tmp_path / "sessions.sqlite3"
    before = _a_store_with_rows(domain)

    report = split_stores(domain)

    assert report.moved[table] == before[table]


def test_the_split_applies_no_migration_to_the_domain_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assertable half of the copy-before-drop guarantee, while the drop does not yet exist.

    `split_stores` opens the domain store raw precisely so `open_database`'s pending migrations
    cannot run — once Stage 2 adds migration 14, a migration running here would drop the tables
    this function is in the middle of reading. The ordering test proper arrives with that
    migration; this pins the property it rests on, today.
    """
    domain = tmp_path / "sessions.sqlite3"
    _a_store_with_rows(domain)

    # The mechanism, not the outcome. Asserting `schema_version` is unchanged passes whether
    # this opens raw or through `open_database`, because there is no pending migration today --
    # a check that cannot fail. What discriminates the two is whether the migration machinery is
    # entered at all, so that is what is asserted: with `open_database` this raises.
    real = database.open_database

    def guard(path: Path, **kwargs: object):
        # Scoped to the domain path: `split_stores` legitimately opens the *UI* store through
        # this same function, so refusing every call would fail the honest implementation too
        # — which it did, on the first attempt at this test.
        if Path(path) == domain:
            raise AssertionError("split_stores must not open the domain store for migration")
        return real(path, **kwargs)

    monkeypatch.setattr(database, "open_database", guard)
    split_stores(domain)


def test_a_second_start_takes_no_further_backup(tmp_path: Path) -> None:
    """Stage 1 is additive, so the moved tables stay in the domain store until Stage 2.

    Without this, every process start for the whole of that window wrote a fresh full-database
    copy and nothing removed them — unbounded growth on an otherwise healthy host.
    """
    domain = tmp_path / "sessions.sqlite3"
    _a_store_with_rows(domain)

    first = split_stores(domain)
    second = split_stores(domain)
    third = split_stores(domain)

    assert first.backup is not None
    assert second.backup is None and third.backup is None
    assert len(list(tmp_path.glob("*.pre-split-*.bak"))) == 1


def test_two_backups_in_the_same_second_do_not_collide(tmp_path: Path) -> None:
    """Up to five writers across four processes, and the stamp resolves to the second."""
    domain = tmp_path / "sessions.sqlite3"
    _a_store_with_rows(domain)
    from remote_agents.adapters.sqlite.store_split import _backup

    connection = sqlite3.connect(domain)
    try:
        names = {_backup(connection, domain).name for _ in range(5)}
    finally:
        connection.close()

    assert len(names) == 5
