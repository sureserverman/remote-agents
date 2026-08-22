"""One writer at a time over the console's panes — across processes, not only within one.

The console is arranged by whoever is holding it, and until now that was always one process:
the local surface. `ConsoleComposer` guarded every arrangement decision with an `asyncio.Lock`
because each of them reads the arrangement and then acts on it two or more awaited round trips
later, so two overlapping callers *inside one process* could both decide against the same stale
reading.

**A second process now arranges it too.** The bot's stop path steps the console aside before it
destroys a pane, the same way the local surface's does, so the owner stopping a displayed
session from their phone gets the projects list back immediately instead of watching the console
sit a pane short until its next reload. That was DEC-005's reason for keeping the bot out — one
writer, by construction — and the reason is answered here rather than accepted: an `asyncio.Lock`
is per-process and says nothing about the other one, and what two processes deciding from the
same stale reading produce is not a cosmetic glitch. `ConsoleComposer._restore_stale_display`
spells out the worst of it: the two remembered pane ids come to name entirely different places,
and swapping them blindly puts a **live agent's pane into another session's window**, where a
stop of that other session destroys it.

So the lock has two halves and they are taken in one order, always: the in-process lock first,
the file lock second. Reversed, one process's two coroutines would both be waiting on a
`flock` — which conflicts between *open file descriptions*, so a second one from the same
process blocks exactly as another process's would — and the in-process lock would never get the
chance to serialise them cheaply.

**It is bounded, and giving up is a normal outcome.** The console is presentation and DEC-006
says a display that fails costs the arrangement and never a session — so waiting forever on a
peer that has wedged is the one thing this may not do. `ConsoleBusy` is raised instead, every
caller in the composer already degrades on an exception, and `SessionService._leave_the_console`
bounds the whole call again from outside.

**A lock that cannot be created is not a failure either.** A composition with nowhere to put the
file — a test, a scratch console, a host whose state directory is not writable — gets the
in-process half alone, which is exactly the guarantee this project had before. That is a
deliberate degradation rather than an oversight, and it is the reason `path` is optional.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
from pathlib import Path
from typing import IO

_LOG = logging.getLogger(__name__)

_ACQUIRE_TIMEOUT_SECONDS = 5.0
"""How long to wait for the other process before giving up on arranging the console.

Generous against what it guards — the operations under this lock are a handful of tmux calls
and none of them waits on an agent — so reaching it means a peer has wedged rather than that
the console is busy. Deliberately longer than `services._CONSOLE_HIDE_TIMEOUT_SECONDS`, which
bounds the *bot's* step-aside from outside at two seconds: the outer bound should be the one
that fires on the stop path, so a stop is never held up by this for longer than the stop path
itself allows.
"""

_POLL_SECONDS = 0.02
"""How often to retry the file lock while waiting.

Polled rather than blocking, because `flock` without `LOCK_NB` blocks the whole event loop —
and this loop is also serving the owner's screen, a Telegram long poll, or both.
"""


class ConsoleBusy(RuntimeError):
    """The other process was arranging the console and did not finish in time."""


class ConsoleArrangementLock:
    """Serialise console arrangement within this process and against every other one."""

    def __init__(
        self, path: Path | None = None, *, timeout: float = _ACQUIRE_TIMEOUT_SECONDS
    ) -> None:
        self._path = path
        self._timeout = timeout
        self._local = asyncio.Lock()
        self._handle: IO[str] | None = None
        self._warned = False

    async def __aenter__(self) -> ConsoleArrangementLock:
        await self._local.acquire()
        try:
            self._handle = await self._claim()
        except BaseException:
            self._local.release()
            raise
        return self

    async def __aexit__(self, *_exception: object) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        self._local.release()

    async def _claim(self) -> IO[str] | None:
        """Take the file half, or answer None when there is no file half to take.

        Opened per acquisition rather than once at construction, and that is not laziness: a
        held `flock` belongs to the open file description, so releasing it means closing the
        handle that owns it. Keeping one open for the object's life would make release and
        re-acquire share a description and quietly stop excluding anything.
        """
        if self._path is None:
            return None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._path.open("a+")
        except OSError:
            if not self._warned:
                # Once per object. This runs on every arrangement, and a console whose lock
                # file is unwritable would otherwise repeat the line for as long as it is open.
                self._warned = True
                _LOG.warning(
                    "cannot use %s to coordinate console panes with the other surface; "
                    "arranging with this process's lock alone",
                    self._path,
                )
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except OSError:
                if loop.time() >= deadline:
                    handle.close()
                    raise ConsoleBusy("the other surface is still arranging the console") from None
                await asyncio.sleep(_POLL_SECONDS)
