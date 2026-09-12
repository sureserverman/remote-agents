"""A watcher that says the store changed, without opening it.

Every session list on both surfaces discovers new rows by *asking*: the local surface on a
timer, the phone when the owner presses. A session another process wrote is therefore
invisible until the next ask, which is the delay this exists to remove.

**It watches the database's files rather than the database.** DEC-035 gives a surface its
store handle for one operation, and watching is not one -- the obvious alternative, comparing
`PRAGMA data_version` across polls, needs a connection held open for the life of the process,
which is exactly the lease that decision refuses. `stat` on two paths costs no connection, no
lock, and nothing the writer can contend with.

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

    def subscribe(self, listener: object) -> Unsubscribe:
        """Register `listener`; the returned callable detaches it and is idempotent."""
        typed: Callable[[StoreChanged], None] = listener  # type: ignore[assignment]
        self._listeners.append(typed)

        def unsubscribe() -> None:
            # Idempotent by construction rather than by the caller remembering: the port
            # promises a second call is a no-op, and an error path may well call it twice.
            if typed in self._listeners:
                self._listeners.remove(typed)

        return unsubscribe

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
        """Ask `run` to return after its current sleep. Idempotent."""
        self._running = False


def _fingerprint(path: Path) -> _Fingerprint:
    try:
        status = os.stat(path)
    except OSError:
        # Missing, or unreadable for now. Both are "no reading", and both compare equal to
        # themselves, so a file that stays absent is not a change and one that appears is.
        return None
    return (status.st_mtime_ns, status.st_size)
