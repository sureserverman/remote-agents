"""Keep the console's tabs equal to the live sessions, and never touch lifecycle.

The one exception allowed out of here is `open`'s last-resort direct switch, and its
documented catcher is the surface's `_open_or_leave`, which treats it as presentation —
announce and stay — never as lifecycle.

The composer is pure presentation policy over `ConsolePort`: which sessions deserve a tab
(RUNNING and STARTING — the ones with a pane worth reaching), when a tab goes (its session
is no longer live), and how a session is opened (select its tab, so the client stays in
the console session where the tab bar and the jump-home binding mean something; fall back
to a direct client switch when tabs cannot be resolved).

Two rules are load-bearing. **Console failure degrades, never dictates** — `ensure` and
`sync` catch, log, and return, and `open`'s tab route does the same before falling back
to a direct client switch; that final switch is the one call allowed to raise out of
here, because "the session could not be reached at all" is the caller's to announce.
Nothing that escapes is ever treated as lifecycle: a broken console costs the owner the
tab bar, never a launch, a stop, or a record (DEC-006 applied to presentation). And
**the composer writes no records** — it reads the caller's session projections and
mutates only windows.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from remote_agents.domain.models import SessionId, SessionRecord, SessionState
from remote_agents.ports.console import ConsolePort
from remote_agents.ports.terminal import TerminalTargetMissing

_LOG = logging.getLogger(__name__)

#: Root-table key that returns the client to the dashboard window from any tab. A root
#: binding costs every pane this key, so it is a function key nothing curated uses.
JUMP_HOME_KEY = "F12"

#: The states whose sessions have a pane worth a tab. ENDED/FAILED panes may linger as
#: PRESERVED evidence, but a tab is an invitation to work, not an archive.
_TAB_STATES = frozenset({SessionState.RUNNING, SessionState.STARTING})


class ConsoleComposer:
    """Create, reconcile, and focus the console's tabs; degrade to nothing on failure."""

    def __init__(
        self,
        console: ConsolePort,
        dashboard_command: tuple[str, ...],
        working_directory: Path,
        *,
        jump_home_key: str = JUMP_HOME_KEY,
    ) -> None:
        self._console = console
        self._dashboard_command = dashboard_command
        self._working_directory = working_directory
        self._jump_home_key = jump_home_key
        # One lock over every link decision. sync() derives its to-link set from a windows
        # snapshot, and open() links on a miss — two awaited round-trips apart, so without
        # the lock a launch completing during a periodic sync could link the same session
        # twice (two tabs, one owner; self-healing on the session's end, but visible).
        self._links = asyncio.Lock()

    async def ensure(self) -> bool:
        """Make the console exist with the binding installed; say whether it is usable."""
        try:
            if not await self._console.console_exists():
                await self._console.create_console(
                    self._dashboard_command, self._working_directory
                )
            await self._console.install_console_binding(self._jump_home_key)
        except Exception:
            _LOG.exception("the console could not be ensured; the surface degrades")
            return False
        return True

    async def sync(self, records: tuple[SessionRecord, ...]) -> None:
        """Link a tab per live session, unlink tabs whose session is gone; idempotent.

        An unattributable tab — index above zero, no owner mark — is left alone: a window
        somebody created by hand in the console is not this composer's to remove.
        """
        try:
            live = {
                record.session_id for record in records if record.state in _TAB_STATES
            }
            async with self._links:
                windows = await self._console.console_windows()
                linked = {owner for _, owner in windows if owner is not None}
                for session_id in live - linked:
                    await self._console.link_session_window(session_id)
            for index, owner in windows:
                if owner is not None and owner not in live:
                    await self._unlink_quietly(index)
        except Exception:
            _LOG.exception("console tab sync failed; tabs may lag until the next pass")

    async def open(self, session_id: SessionId) -> None:
        """Focus one session: its tab if it has or can get one, a direct switch if not.

        The tab is preferred because selecting it keeps the client in the console session,
        where the tab bar shows every session and the jump-home binding works. The direct
        switch is the degraded route, not an equal one — but a session the owner asked to
        open is reached even when the console is broken.
        """
        try:
            async with self._links:
                index = await self._tab_index(session_id)
                if index is None:
                    await self._console.link_session_window(session_id)
                    index = await self._tab_index(session_id)
            if index is not None:
                await self._console.select_console_window(index)
                return
        except Exception:
            _LOG.exception("opening by tab failed; switching the client directly")
        await self._console.switch_client_to_session(session_id)

    async def _tab_index(self, session_id: SessionId) -> int | None:
        for index, owner in await self._console.console_windows():
            if owner == session_id:
                return index
        return None

    async def _unlink_quietly(self, index: int) -> None:
        try:
            await self._console.unlink_console_window(index)
        except TerminalTargetMissing:
            pass  # already gone is exactly what sync wanted
