"""One writer of keystrokes per managed pane at a time -- across processes, not only within one.

Three things type into a managed pane: the stop sequence, the Remote Control toggle and, since
DEC-099, the prompt relay. They run in two processes -- the bot's service and the local surface
-- so two sequences sent at once used to interleave keystroke by keystroke in one pane (BL-056).
A relayed prompt makes that worse than untidy: a paste landing between a stop's keys, or an
`Enter` landing between a paste and its check, types into something nobody checked.

The shape is `application/console_lock.py`'s, for its reasons: an in-process lock first, then a
`flock` on a file, always in that order; polled rather than blocking, because `flock` without
`LOCK_NB` blocks the event loop; bounded, because a peer that has wedged must cost a named
failure and not a hang; and a composition with nowhere to put the file keeps the in-process half,
which is exactly the guarantee this project had before.

**One lock per session, not one for the server.** Typing into one pane must never wait on
another session's stop, so the file is named for the session.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from pathlib import Path
from typing import IO

from remote_agents.domain.models import SessionId

_LOG = logging.getLogger(__name__)

_ACQUIRE_TIMEOUT_SECONDS = 10.0
"""How long to wait for another sender on the same pane before giving up.

Longer than any sequence it guards takes -- a stop is a few keys 150 ms apart, a relay is a paste,
a capture and one key -- so reaching it means a peer has wedged."""

_POLL_SECONDS = 0.02

_LOCAL: dict[str, asyncio.Lock] = {}
"""The in-process half, one per session, shared by every `SessionKeyLock` in this process."""


class KeysBusy(RuntimeError):
    """Another sender held this pane's keys and did not finish in time."""


class SessionKeyLock:
    """Serialise keystrokes into one session's pane within this process and against others."""

    def __init__(
        self,
        directory: Path | None,
        session_id: SessionId,
        *,
        timeout: float = _ACQUIRE_TIMEOUT_SECONDS,
    ) -> None:
        self._path = None if directory is None else directory / f"keys-{session_id}.lock"
        self._local = _LOCAL.setdefault(str(session_id), asyncio.Lock())
        self._timeout = timeout
        self._handle: IO[str] | None = None

    async def __aenter__(self) -> SessionKeyLock:
        try:
            await asyncio.wait_for(self._local.acquire(), self._timeout)
        except TimeoutError:
            raise KeysBusy("another sender in this process still holds the pane") from None
        try:
            self._handle = await self._claim()
        except BaseException:
            self._local.release()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        handle, self._handle = self._handle, None
        try:
            if handle is not None:
                handle.close()
        finally:
            self._local.release()

    async def _claim(self) -> IO[str] | None:
        """Take the file half, or answer None when there is no file half to take.

        Opened per acquisition: a held `flock` belongs to the open file description, so releasing
        it means closing the handle that owns it (`console_lock._claim` carries the argument).
        """
        if self._path is None:
            return None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # O_NOFOLLOW: a link planted at the lock's name is refused, not followed.
            descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            handle = os.fdopen(descriptor, "a+")
        except OSError:
            _LOG.warning(
                "cannot use %s to serialise keystrokes with the other surface; "
                "using this process's lock alone",
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
                    raise KeysBusy("another process is still typing into this pane") from None
                await asyncio.sleep(_POLL_SECONDS)
