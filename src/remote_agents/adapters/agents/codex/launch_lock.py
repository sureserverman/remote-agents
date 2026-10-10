"""Codex launches share the daemon; a Remote Control change holds it alone (BL-037).

**What it guards, measured rather than reasoned.** A plain `codex` launch starts the managed
app-server daemon itself (`daemon_auto_start` is a stable feature, on by default). Every verb
that changes the Remote Control preference -- `remote-control start`, `app-server daemon
enable-remote-control`, `disable-remote-control` -- stops that backend and starts a new one. A
TUI past its daemon handshake reconnects to the new one; a TUI still inside it exits
(`docs/acceptance-2026-10-codex-enable-race.md`). So a launch and a change must not overlap.
The launch side holds this from before its pane exists until its handshake is drawn. The
change side holds it for the whole command.

**Shared for launches, exclusive for changes.** Launches do not race each other, only a change,
so any number of them hold it together and a change waits for all of them; a change holds it
alone. Serialising launches against each other would refuse a second launch for two seconds
behind a slow first one, with a reason that names a change nobody made.

The shape is otherwise `adapters/tmux/key_lock.py`'s: an in-process half first, then a `flock`
on a file (`LOCK_SH` or `LOCK_EX`), always in that order; polled rather than blocking, because
`flock` without `LOCK_NB` blocks the event loop; bounded, because a peer that has wedged must
cost a named refusal and not a hang; and a composition with nowhere to put the file keeps the
in-process half. Unlike the key lock, the bound is the caller's: an owner's launch waits two
seconds, a change waits for the launches to finish starting.

**A launch from outside remote-agents still races.** Only the two surfaces' own launches take
this lock; a `codex` typed into a terminal does not. That residual is the owner's accepted one
(BL-037's ruling).
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import IO

from remote_agents.ports.host_remote_control import CodexRemoteControlBusy

_LOG = logging.getLogger(__name__)

_POLL_SECONDS = 0.02


class _Holders:
    """The in-process half for one lock file: how many share it, and whether one holds it alone."""

    def __init__(self) -> None:
        self.shared = 0
        self.exclusive = False
        self.changed = asyncio.Condition()


_LOCAL: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _Holders]] = (
    weakref.WeakKeyDictionary()
)
"""Per event loop, then per lock file. Per loop because an `asyncio.Condition` belongs to the loop
that first waits on it; a process runs one loop, but a test suite runs many."""


class CodexLaunchLock:
    """Let Codex launches share the daemon, and a Remote Control change hold it alone."""

    def __init__(self, path: Path | None) -> None:
        self._path = path

    @asynccontextmanager
    async def hold(self, wait: float, *, alone: bool) -> AsyncIterator[None]:
        """Hold the lock for the block, or raise `CodexRemoteControlBusy` after `wait` seconds.

        `alone=False` for a launch, resume or trust answer; `True` for a change.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        holders = _LOCAL.setdefault(loop, {}).setdefault(str(self._path), _Holders())

        def free() -> bool:
            return not holders.exclusive and (holders.shared == 0 or not alone)

        async with holders.changed:
            try:
                await asyncio.wait_for(holders.changed.wait_for(free), wait)
            except TimeoutError:
                raise CodexRemoteControlBusy(
                    f"the Codex daemon was still busy in this process after {wait:g}s"
                ) from None
            if alone:
                holders.exclusive = True
            else:
                holders.shared += 1
        try:
            handle = await self._claim(deadline, wait, alone)
            try:
                yield
            finally:
                if handle is not None:
                    handle.close()
        finally:
            async with holders.changed:
                if alone:
                    holders.exclusive = False
                else:
                    holders.shared -= 1
                holders.changed.notify_all()

    async def _claim(self, deadline: float, wait: float, alone: bool) -> IO[str] | None:
        """Take the file half, or answer None when there is no file half to take.

        Opened per acquisition: a held `flock` belongs to the open file description, so
        releasing it means closing the handle that owns it (`console_lock._claim` carries the
        argument).
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
                "cannot use %s to serialise Codex launches with the other surface; "
                "using this process's lock alone",
                self._path,
            )
            return None
        loop = asyncio.get_running_loop()
        while True:
            try:
                mode = fcntl.LOCK_EX if alone else fcntl.LOCK_SH
                fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
                return handle
            except OSError:
                if loop.time() >= deadline:
                    handle.close()
                    raise CodexRemoteControlBusy(
                        f"another process held the Codex daemon after {wait:g}s"
                    ) from None
                await asyncio.sleep(_POLL_SECONDS)
