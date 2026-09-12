"""The watcher notices a write made by a connection it does not own.

This is the claim the whole design rests on and the one a unit test cannot make: the unit
tests write bytes to files, while a real store is written through SQLite in WAL mode, where a
commit lands in `-wal` and the database file itself may not be touched until a checkpoint.
If watching those two paths did not see a genuine commit, the watcher would be correct about
files and useless about sessions.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from remote_agents.adapters.sqlite.database import watched_paths
from remote_agents.application.store_watch import StoreWatch


def _write_through_a_second_connection(database: Path, value: str) -> None:
    """Exactly what another process does: its own connection, its own transaction."""
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
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


async def test_a_commit_is_seen_while_the_writer_still_holds_its_connection(
    tmp_path: Path,
) -> None:
    """The case the test above cannot make, and the one that argues for watching `-wal`.

    A connection that *closes* checkpoints on the way out, which touches the database file --
    so the test above passes even against a watcher that ignores `-wal` entirely, and cannot
    be the evidence for watching it. A long-lived writer is what this project actually has:
    the daemon holds its connection open for its whole life, so a launch's commit lands in
    `-wal` and may leave the database file untouched for a long time.

    Mutation-checked: watching only the database file turns this red and leaves the one above
    green, which is the whole reason both exist.
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

        writer.execute("INSERT INTO sessions (id) VALUES ('while-held')")
        writer.commit()
        await watch.poll_once()

        assert (tmp_path / "sessions.sqlite3-wal").exists(), (
            "the premise: an open connection in WAL mode keeps its log on disk"
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
