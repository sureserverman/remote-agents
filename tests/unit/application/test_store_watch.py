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
    watch = StoreWatch(_paths(tmp_path), interval=0.001)
    seen: list[StoreChanged] = []
    # `subscribe` starts the loop itself at a real interval, so this test drives the thing the
    # surfaces actually get rather than a hand-started one.
    detach = watch.subscribe(seen.append)
    task = watch._task
    assert task is not None

    for _ in range(200):
        await asyncio.sleep(0.005)
        if watch._seen is not None:
            break
    database.write_bytes(b"two")
    for _ in range(200):
        await asyncio.sleep(0.005)
        if seen:
            break

    detach()
    await asyncio.sleep(0)

    assert len(seen) >= 1, "the loop must poll on its own, not only when asked"
    assert task.cancelled() or task.done(), "and the last unsubscribe must end it"


async def test_subscribing_starts_the_loop_and_the_last_unsubscribe_stops_it(
    tmp_path: Path,
) -> None:
    """Lifecycle tied to listeners, so no composition has to remember a start call.

    This project already carries the scar the alternative leaves: a repaint interval installed
    one line too late, and a pane that sat frozen for the life of the process with no error
    anywhere. A watcher nobody listens to has nothing to do and one somebody listens to must be
    running, so the two are made true by construction rather than by a startup step.
    """
    database, _wal = _paths(tmp_path)
    database.write_bytes(b"one")
    # A real interval, because this test is about the loop existing. `interval=0` means "the
    # caller drives me" and deliberately starts nothing -- see `_ensure_running`.
    watch = StoreWatch(_paths(tmp_path), interval=0.01)

    assert watch._task is None

    first = watch.subscribe(lambda _change: None)
    assert watch._task is not None and not watch._task.done()

    second = watch.subscribe(lambda _change: None)
    running = watch._task
    assert running is watch._task, "a second listener must not start a second loop"

    first()
    assert watch._task is running, "one listener leaving is not the last one leaving"

    second()
    await asyncio.sleep(0)
    assert watch._task is None


def test_subscribing_outside_a_running_loop_is_idle_rather_than_an_error(tmp_path: Path) -> None:
    """The composition root wires this up before anything is running.

    Raising there would be the one outcome nobody has a fallback for: the surfaces read a
    wired watcher as working and a missing one as "use the interval", and a third case where
    *wiring it breaks startup* fits neither.
    """
    watch = StoreWatch(_paths(tmp_path), interval=0.01)

    detach = watch.subscribe(lambda _change: None)

    assert watch._task is None
    detach()


def test_a_zero_interval_means_the_caller_drives_and_starts_nothing(tmp_path: Path) -> None:
    """Measured, not reasoned: honouring zero literally cost the suite 97 seconds.

    `run()` at a zero interval is `while True: await sleep(0)` — a busy spin for as long as
    anything is subscribed. Every test that subscribed left one behind, and the suite went
    from 100 s to 197 s. Zero now means the caller polls; production passes a real interval.
    """
    watch = StoreWatch(_paths(tmp_path), interval=0)

    detach = watch.subscribe(lambda _change: None)

    assert watch._task is None, "a zero interval must not start a spinning loop"
    detach()
