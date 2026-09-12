"""The watcher notices a write made by a connection it does not own.

This is the claim the whole design rests on and the one a unit test cannot make: the unit
tests write bytes to files, while a real store is written through SQLite.

**Both of the original tests here set `PRAGMA journal_mode=WAL`, and production does not.**
Nothing in `src/` sets `journal_mode`, so the default stands -- the live database reports
`delete`, and no `-wal` has ever existed beside it. So the pair proved the watcher under a
mode this project never runs in, and the one written specifically to *discriminate* the `-wal`
path discriminated a case that does not occur. Recorded plainly because it is the third time
in this stage's history that a test turned out to prove something other than its docstring
claimed.

What the file covers now: the production mode first, on its own, and WAL second and clearly
labelled as insurance for a mode that may be enabled later rather than as evidence about
today.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from remote_agents.adapters.sqlite.database import watched_paths
from remote_agents.application.store_watch import StoreWatch


def _write_through_a_second_connection(database: Path, value: str) -> None:
    """Exactly what another process does: its own connection, its own transaction.

    No `journal_mode` pragma, so this runs in the mode production runs in.
    """
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO sessions (id) VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def test_watched_paths_names_the_database_and_its_write_ahead_log(tmp_path: Path) -> None:
    database = tmp_path / "sessions.sqlite3"

    assert watched_paths(database) == (database, tmp_path / "sessions.sqlite3-wal")


async def test_a_commit_from_another_connection_is_seen(tmp_path: Path) -> None:
    database = tmp_path / "sessions.sqlite3"
    _write_through_a_second_connection(database, "first")
    watch = StoreWatch(watched_paths(database), interval=0)
    seen = []
    watch.subscribe(seen.append)
    await watch.poll_once()

    _write_through_a_second_connection(database, "second")
    await watch.poll_once()

    assert len(seen) == 1, (
        "a real commit through a foreign connection must move one of the watched files"
    )


async def test_a_commit_is_seen_from_a_long_lived_writer_in_the_mode_production_uses(
    tmp_path: Path,
) -> None:
    """The daemon's actual shape: one connection held open for the life of the process.

    The test above closes its connection, and a close does work a long-lived writer never
    does. This one keeps the handle, which is what `serve` does -- so it is the one that says
    the watcher sees a commit from the writer this project really has.

    No `journal_mode` pragma, deliberately. An earlier version set WAL here and argued that
    the commit would land in `-wal`; production sets no journal mode at all, so that argument
    described a database this project does not create.
    """
    database = tmp_path / "sessions.sqlite3"
    writer = sqlite3.connect(database)
    try:
        writer.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        writer.commit()
        watch = StoreWatch(watched_paths(database), interval=0)
        seen = []
        watch.subscribe(seen.append)
        await watch.poll_once()

        writer.execute("INSERT INTO sessions (id) VALUES ('while-held')")
        writer.commit()
        await watch.poll_once()

        assert not (tmp_path / "sessions.sqlite3-wal").exists(), (
            "the premise this file used to get wrong: no journal mode is set, so there is no "
            "-wal, and every change rides on the database file itself"
        )
        assert len(seen) == 1
    finally:
        writer.close()


async def test_a_store_nobody_writes_to_stays_quiet(tmp_path: Path) -> None:
    database = tmp_path / "sessions.sqlite3"
    _write_through_a_second_connection(database, "first")
    watch = StoreWatch(watched_paths(database), interval=0)
    seen = []
    watch.subscribe(seen.append)

    for _ in range(5):
        await watch.poll_once()

    assert seen == [], "reading a store must not be mistaken for writing one"


async def test_the_watcher_still_works_if_wal_is_ever_turned_on(tmp_path: Path) -> None:
    """Insurance, labelled as insurance.

    `watched_paths` names the `-wal` even though nothing creates one, so that enabling WAL
    later does not silently move the signal to a file nobody is watching. This test is the
    only reason to believe that, and it must not be mistaken for evidence about today --
    which is exactly what its predecessor was.
    """
    database = tmp_path / "sessions.sqlite3"
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        writer.commit()
        watch = StoreWatch(watched_paths(database), interval=0)
        seen = []
        watch.subscribe(seen.append)
        await watch.poll_once()

        writer.execute("INSERT INTO sessions (id) VALUES ('under-wal')")
        writer.commit()
        await watch.poll_once()

        assert (tmp_path / "sessions.sqlite3-wal").exists(), "the premise of this test"
        assert len(seen) == 1
    finally:
        writer.close()
