"""The store-change watcher: what it publishes, when, and what it refuses to hold.

`StoreWatch` exists because every list on both surfaces discovers new sessions by *asking* —
a 10 s timer on the local surface, a press on the phone — and a session another process wrote
is therefore invisible for as long as the tick it missed. Watching the database's own files is
the cheapest thing that can say "something changed" without opening the database, which is the
constraint DEC-035 puts on anything that is not performing an operation.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.application.store_watch import StoreWatch
from remote_agents.ports.state_events import StoreChanged


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return (tmp_path / "sessions.sqlite3", tmp_path / "sessions.sqlite3-wal")


async def _settle(watch: StoreWatch, *, ticks: int) -> None:
    """Let the loop run `ticks` polls, with the interval driven to nothing."""
    for _ in range(ticks):
        await asyncio.sleep(0)
        await watch.poll_once()


def test_the_vocabulary_is_one_frozen_record_carrying_when() -> None:
    """`StateEvents` deliberately left `StateChange` undecided until a consumer arrived.

    This is that consumer, and what it needs is the *fact* of a change rather than its
    content: the watcher reads file metadata and genuinely cannot say what changed, so a
    record that carried a session id would be inventing one. Every subscriber re-reads.
    """
    moment = datetime.now(UTC)
    change = StoreChanged(at=moment)

    assert change.at is moment
    with pytest.raises(Exception):
        change.at = moment  # type: ignore[misc]


async def test_a_change_to_the_database_file_publishes_exactly_one_event(tmp_path: Path) -> None:
    database, _wal = _paths(tmp_path)
    database.write_bytes(b"one")
    watch = StoreWatch(_paths(tmp_path), interval=0)
    seen: list[StoreChanged] = []
    watch.subscribe(seen.append)

    await watch.poll_once()
    assert seen == [], "the first poll establishes a baseline rather than reporting a change"

    database.write_bytes(b"two")
    await watch.poll_once()
    await watch.poll_once()

    assert len(seen) == 1, "one change, one event -- a second poll must not repeat it"
    assert isinstance(seen[0], StoreChanged)


async def test_a_change_to_the_write_ahead_log_counts_too(tmp_path: Path) -> None:
    """Most of the time this is the only file that moves.

    In WAL mode a committed write lands in `-wal` and the database file itself is untouched
    until a checkpoint. Watching only the database would therefore miss nearly every change,
    which is the whole reason `watched_paths` names two files.
    """
    database, wal = _paths(tmp_path)
    database.write_bytes(b"one")
    watch = StoreWatch(_paths(tmp_path), interval=0)
    seen: list[StoreChanged] = []
    watch.subscribe(seen.append)
    await watch.poll_once()

    wal.write_bytes(b"a commit")
    await watch.poll_once()

    assert len(seen) == 1


async def test_an_idle_store_publishes_nothing(tmp_path: Path) -> None:
    database, _wal = _paths(tmp_path)
    database.write_bytes(b"one")
    watch = StoreWatch(_paths(tmp_path), interval=0)
    seen: list[StoreChanged] = []
    watch.subscribe(seen.append)

    for _ in range(5):
        await watch.poll_once()

    assert seen == []


async def test_a_missing_write_ahead_log_is_not_an_error(tmp_path: Path) -> None:
    """A store that has never been written has no `-wal`, and neither has a checkpointed one.

    Absence is a reading, not a failure: it is recorded as `None` and compared like any other
    value, so the file appearing later is itself the change it should be.
    """
    database, wal = _paths(tmp_path)
    database.write_bytes(b"one")
    watch = StoreWatch(_paths(tmp_path), interval=0)
    seen: list[StoreChanged] = []
    watch.subscribe(seen.append)

    await watch.poll_once()
    assert not wal.exists()

    wal.write_bytes(b"now it exists")
    await watch.poll_once()

    assert len(seen) == 1, "a file appearing is a change"


async def test_a_missing_database_is_not_an_error_either(tmp_path: Path) -> None:
    watch = StoreWatch(_paths(tmp_path), interval=0)
    seen: list[StoreChanged] = []
    watch.subscribe(seen.append)

    await watch.poll_once()
    await watch.poll_once()

    assert seen == []


async def test_the_watcher_opens_no_database_connection(monkeypatch, tmp_path: Path) -> None:
    """DEC-035: a surface holds its store handle for one operation, and this is not one.

    Asserted by making `sqlite3.connect` raise rather than by reading the source, because the
    obvious alternative implementation — comparing `PRAGMA data_version` — requires a
    connection held open across polls, which is precisely the lease this decision forbids.
    """

    def refuse(*arguments: object, **keywords: object) -> None:
        raise AssertionError("the watcher must not open the database")

    monkeypatch.setattr(sqlite3, "connect", refuse)
    database, _wal = _paths(tmp_path)
    database.write_bytes(b"one")
    watch = StoreWatch(_paths(tmp_path), interval=0)
    seen: list[StoreChanged] = []
    watch.subscribe(seen.append)

    await watch.poll_once()
    database.write_bytes(b"two")
    await watch.poll_once()

    assert len(seen) == 1


async def test_unsubscribing_detaches_the_listener_and_is_idempotent(tmp_path: Path) -> None:
    database, _wal = _paths(tmp_path)
    database.write_bytes(b"one")
    watch = StoreWatch(_paths(tmp_path), interval=0)
    seen: list[StoreChanged] = []
    detach = watch.subscribe(seen.append)
    await watch.poll_once()

    detach()
    detach()
    database.write_bytes(b"two")
    await watch.poll_once()

    assert seen == []


async def test_one_listener_raising_does_not_cost_the_others_their_event(tmp_path: Path) -> None:
    """A consumer's bug must not stop a sibling surface from redrawing.

    The watcher is shared by every list in the process, so a listener that throws is contained
    and logged rather than allowed to end the loop -- the same posture the activity feed takes.
    """
    database, _wal = _paths(tmp_path)
    database.write_bytes(b"one")
    watch = StoreWatch(_paths(tmp_path), interval=0)
    seen: list[StoreChanged] = []

    def explode(_change: StoreChanged) -> None:
        raise RuntimeError("this listener is broken")

    watch.subscribe(explode)
    watch.subscribe(seen.append)
    await watch.poll_once()

    database.write_bytes(b"two")
    await watch.poll_once()

    assert len(seen) == 1


async def test_the_loop_polls_until_it_is_stopped(tmp_path: Path) -> None:
    database, _wal = _paths(tmp_path)
    database.write_bytes(b"one")
    watch = StoreWatch(_paths(tmp_path), interval=0)
    seen: list[StoreChanged] = []
    watch.subscribe(seen.append)

    task = asyncio.create_task(watch.run())
    await asyncio.sleep(0)
    database.write_bytes(b"two")
    for _ in range(20):
        await asyncio.sleep(0)
        if seen:
            break
    watch.stop()
    await asyncio.wait_for(task, timeout=5)

    assert len(seen) >= 1
    assert task.done()
