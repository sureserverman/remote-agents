"""A watcher that says the store changed, without opening it.

Every session list on both surfaces discovers new rows by *asking*: the local surface on a
timer, the phone when the owner presses. A session another process wrote is therefore
invisible until the next ask, which is the delay this exists to remove.

**It watches the database's files rather than the database.** DEC-035 gives a surface its
store handle for one operation, and watching is not one -- the obvious alternative, comparing
`PRAGMA data_version` across polls, needs a connection held open for the life of the process,
which is exactly the lease that decision refuses. `stat` on two paths costs no connection, no
lock, and nothing the writer can contend with.

**Which file actually carries the signal, corrected.** This module first argued that a commit
lands in `-wal` and may leave the database untouched for a long time. That is true of SQLite
in WAL mode and this project does not use it: nothing in `src/` sets `journal_mode`, the live
store reports `delete`, and no `-wal` has ever existed beside it. Every change is a write to
the database file, which is what the fingerprints below actually see. `watched_paths` still
names the `-wal` so the watcher stays correct if the mode is ever changed; today it is a
permanently absent path costing one `stat`.

What it publishes is deliberately thin. `StoreChanged` carries a time and nothing else,
because file metadata genuinely cannot say *what* changed: a record naming a session id would
be inventing one. Every subscriber re-reads, which is what they would have done on their timer
anyway -- the watcher only tells them when it is worth doing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from remote_agents.ports.state_events import StoreChanged, Unsubscribe

_LOG = logging.getLogger(__name__)

#: How often the files are stated. A second is well under the ten the local surface's timer
#: used to impose and well over the cost of two `stat` calls, which is what makes "poll" the
#: right shape here rather than an inotify watch: one syscall pair per second per process is
#: cheaper than a watch descriptor's portability problems, and this project runs on macOS too.
DEFAULT_INTERVAL_SECONDS = 1.0

#: One file's reading: its size and modification time in nanoseconds, or `None` when it is not
#: there. Absence is a value rather than an error, so a `-wal` appearing is itself a change --
#: which is what a store being written for the first time looks like.
#:
#: **What this can miss, stated rather than left to be discovered.** Two distinct writes
#: fingerprint identically if they land in the same mtime tick *and* leave the file the same
#: size. On the filesystems this project runs on -- ext4 and APFS, both nanosecond -- that
#: needs two writes within one tick, which a poll a second apart will not straddle in
#: practice. It is a real gap in the model rather than an impossibility, and the cost if it
#: ever happens is bounded: the surfaces keep a sixty-second fallback and the bot's page is
#: redrawn by the next change, so a missed reading is a late list, not a wrong one. If it is
#: ever observed, `st_ino` and `st_ctime_ns` are the next two fields to add.
_Fingerprint = tuple[int, int] | None


class StoreWatch:
    """Polls the store's files and publishes `StoreChanged` when their metadata moves.

    Implements `ports.state_events.StateEvents`, which held its `StateChange` vocabulary open
    for the first consumer to decide. This is that consumer.
    """

    def __init__(
        self,
        paths: Iterable[Path],
        *,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._paths = tuple(paths)
        self._interval = interval
        self._now = now
        self._listeners: list[Callable[[StoreChanged], None]] = []
        self._seen: tuple[_Fingerprint, ...] | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None

    def subscribe(self, listener: object) -> Unsubscribe:
        """Register `listener`; the returned callable detaches it and is idempotent.

        **Subscribing starts the loop, and the last unsubscribe stops it.** The alternative --
        a host calling `run()` somewhere in its startup -- is a step every composition has to
        remember, and this project already carries a comment about a repaint interval installed
        one line too late and a pane that sat frozen for the life of the process with no error
        anywhere. A watcher nobody listens to has nothing to do, and one somebody listens to
        must be running: tying the two together makes both true by construction.

        It also keeps `ports.state_events.StateEvents` at one method. Lifecycle is this
        implementation's business -- a future adapter that streams from a socket has entirely
        different lifecycle and the same `subscribe`.
        """
        typed: Callable[[StoreChanged], None] = listener  # type: ignore[assignment]
        self._listeners.append(typed)
        self._ensure_running()

        def unsubscribe() -> None:
            # Idempotent by construction rather than by the caller remembering: the port
            # promises a second call is a no-op, and an error path may well call it twice.
            if typed in self._listeners:
                self._listeners.remove(typed)
            if not self._listeners:
                self.stop()

        return unsubscribe

    def _ensure_running(self) -> None:
        """Start the poll loop if it is not already running and there is a loop to run it on.

        A `subscribe` from outside a running loop -- a composition root wiring things up before
        `run()` is called, which is exactly what this project's does -- cannot create a task.
        That is not an error: it leaves the watcher idle, and the next subscribe from inside a
        loop starts it. What it must not do is raise, because the surfaces treat a wired
        watcher as working and a missing one as "fall back to the interval"; a third outcome
        where wiring it *breaks startup* is the one nobody has a fallback for.
        """
        if self._interval <= 0:
            # **Zero means "the caller drives me", not "poll as fast as you can".** A loop that
            # honoured a zero interval literally would be `while True: await sleep(0)`, which
            # is a busy spin on the event loop for as long as anything is subscribed --
            # measured, it took the test suite from 100 s to 197 s before this branch existed,
            # because every test that subscribed left one spinning behind it. Tests that want
            # determinism call `poll_once` themselves; production passes a real interval.
            return
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Logged rather than passed over in silence. A watcher that never started looks
            # exactly like one that works -- the surfaces just fall back to their interval,
            # which this stage lengthened from ten seconds to sixty, so the degraded path is
            # now *six times worse* than the behaviour it replaced. Something has to say so.
            _LOG.debug("no running loop at subscribe; the store watch will start on the next")
            return
        self._running = True
        self._task = loop.create_task(self.run())

    async def poll_once(self) -> bool:
        """Take one reading and publish if it moved. Returns whether anything was published.

        **The first poll establishes a baseline and publishes nothing.** A watcher that fired
        on its first reading would make every mount of every list redraw itself immediately
        for no reason, which is indistinguishable from the bug it is here to fix.
        """
        reading = tuple(_fingerprint(path) for path in self._paths)
        if self._seen is None:
            self._seen = reading
            return False
        if reading == self._seen:
            return False
        self._seen = reading
        self._publish(StoreChanged(at=self._now()))
        return True

    def _publish(self, change: StoreChanged) -> None:
        # Each listener is called inside its own guard. This watcher is shared by every list
        # in the process, so a consumer's bug must not cost a sibling surface its redraw --
        # and must not end the loop, which would silently restore the delay this removes.
        for listener in tuple(self._listeners):
            try:
                listener(change)
            except Exception:
                _LOG.exception("a state-change listener raised; the others still ran")

    async def run(self) -> None:
        """Poll until `stop`. The caller owns the task and its lifetime."""
        self._running = True
        while self._running:
            try:
                await self.poll_once()
            except Exception:
                # A `stat` that fails -- a state directory unmounted, a permission change --
                # is a bad poll, not a dead watcher. Ending the loop here would put both
                # surfaces back on their timers with nothing saying so.
                _LOG.exception("a store poll failed; the watcher keeps going")
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        """Ask `run` to return after its current sleep, and drop the task. Idempotent.

        The task is cancelled rather than only flagged: `run` sleeps for the interval between
        polls, so a flag alone would leave it alive for up to that long after the last listener
        went away -- and in a test, past the end of the test.
        """
        self._running = False
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()


def _fingerprint(path: Path) -> _Fingerprint:
    try:
        status = os.stat(path)
    except OSError:
        # Missing, or unreadable for now. Both are "no reading", and both compare equal to
        # themselves, so a file that stays absent is not a change and one that appears is.
        return None
    return (status.st_mtime_ns, status.st_size)
