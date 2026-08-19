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
from remote_agents.ports.console import ConsolePort, HostedPane
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
            # Outside the lock, and safe for a structural reason rather than a lucky one:
            # unlinking only ever touches console windows above index 0 (the codec refuses
            # 0), while `show`/`show_projects` only ever exchange panes *within* window 0.
            # The two mechanisms cannot meet. Said here because the lock's own docstring
            # speaks of link decisions and is silent about unlink, which cost one reader a
            # full trace to re-derive.
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

    async def show(self, session_id: SessionId) -> None:
        """Put one agent's pane in the console's left slot, sending whoever is there home.

        **Two exchanges when an agent is already shown, never one.** Swapping the slot's
        current occupant straight against the incoming agent would leave the outgoing agent
        hosted by the incoming one's session: two identities crossed, both processes still
        running, and nothing raising. So the shown agent goes home first — which is exactly
        `show_projects`' exchange — and only then does the new one come in.

        **The slot is re-read between them.** It is a position, not a pane: after the first
        exchange the pane that was in the slot is living in the outgoing agent's window, so
        a remembered id names the wrong place. Sub-plan 1's live drive made this mistake and
        landed its second agent in the first agent's home window.

        Presentation, like everything else here (DEC-036): a session with no pane of its own
        cannot be displayed and is left alone rather than refused loudly, and every failure
        degrades to a log line. Nothing on this path writes a record or touches lifecycle.
        """
        try:
            async with self._links:
                arrangement = await self._console.pane_arrangement()
                slot = _left_slot(arrangement)
                agent = _pane_of(arrangement, session_id)
                if slot is None or agent is None:
                    _LOG.debug("nothing to show for %s; the console is left as it is", session_id)
                    return
                if slot.pane_id == agent.pane_id:
                    return
                if slot.session_id is not None:
                    if not await self._send_home(arrangement, slot):
                        return
                    arrangement = await self._console.pane_arrangement()
                    slot = _left_slot(arrangement)
                    agent = _pane_of(arrangement, session_id)
                    if slot is None or agent is None or slot.session_id is not None:
                        _LOG.warning("the left slot did not free up; %s is not shown", session_id)
                        return
                await self._console.swap_panes(agent.pane_id, slot.pane_id)
        except Exception:
            _LOG.exception(
                "showing %s in the console failed; the arrangement is unchanged", session_id
            )

    async def show_projects(self) -> None:
        """Bring the projects surface back to the left slot, wherever the exchange left it.

        The surface is not tracked; it is *found* — the pane parked in the shown agent's own
        window, which is where the exchange that displaced it put it. An already-resting
        console exchanges nothing, so this is safe to call on any path that wants the surface
        in front, including one that does not know whether an agent is shown.
        """
        try:
            async with self._links:
                arrangement = await self._console.pane_arrangement()
                slot = _left_slot(arrangement)
                if slot is None or slot.session_id is None:
                    return
                await self._send_home(arrangement, slot)
        except Exception:
            _LOG.exception(
                "returning the projects surface failed; the console still shows an agent"
            )

    async def _send_home(self, arrangement: tuple[HostedPane, ...], slot: HostedPane) -> bool:
        """Exchange the slot's agent with the pane parked in that agent's own window."""
        parked = _parked_in(arrangement, slot.session_id)
        if parked is None:
            _LOG.warning(
                "the pane displaced by %s is not identifiable; leaving the console as it is",
                slot.session_id,
            )
            return False
        await self._console.swap_panes(parked.pane_id, slot.pane_id)
        return True

    async def flash(self, text: str) -> None:
        """One status-bar line for news, suppressed while the owner is looking at it.

        The console's current window being 0 means the client rests on the dashboard,
        where the feed pane already shows the same news — flashing there would say one
        thing twice on one screen. Failure degrades to silence: the feed row is the
        durable record, the flash is only a nudge.
        """
        try:
            if await self._console.console_active_window() == 0:
                return
            await self._console.display_message(text)
        except Exception:
            _LOG.exception("the console status flash failed")

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


def _left_slot(arrangement: tuple[HostedPane, ...]) -> HostedPane | None:
    """The console's left slot: lowest pane index in its own window 0, or None.

    Read as a position on every call rather than remembered, because that is the one thing
    an exchange changes about it. Window 0 is named explicitly: under the tab model the
    console also hosts linked windows whose panes belong to their sessions, and the current
    window is whichever one the owner is looking at.
    """
    console_panes = [pane for pane in arrangement if pane.on_console and pane.window_index == 0]
    return min(console_panes, key=lambda pane: pane.pane_index, default=None)


def _pane_of(arrangement: tuple[HostedPane, ...], session_id: SessionId) -> HostedPane | None:
    """The pane carrying one identity in its own right, or None if nothing does."""
    return next((pane for pane in arrangement if pane.session_id == session_id), None)


def _parked_in(arrangement: tuple[HostedPane, ...], session_id: SessionId) -> HostedPane | None:
    """The one unidentified pane sitting in a session's own window, or None if it is unclear.

    That pane is the console surface an exchange displaced. **Exactly one, or none**: an
    operator's hand-split pane makes two candidates, and picking one would be picking by
    listing order — the same wrong basis Sub-plan 1 removed from destruction, with a pane
    swapped into the console in place of the surface as the prize. Refusing leaves the
    console showing the agent, which the owner can see.
    """
    candidates = [
        pane
        for pane in arrangement
        if pane.host == session_id and pane.session_id is None
    ]
    return candidates[0] if len(candidates) == 1 else None
