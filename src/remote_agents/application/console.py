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
from dataclasses import dataclass
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

#: How many exchanges `recover` will make before reporting that the console did not settle.
#: Each pass puts one pane where it belongs, so a console with a handful of agents settles in
#: a handful; the bound exists for the permutation that does not, which must end in a report
#: rather than in a loop.
_RECOVERY_PASSES = 8


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """What a recovery pass did, with what it could not do kept separately.

    Two tuples rather than one list of sentences, because a caller that has to tell "I moved
    this" from "I could not move this" by reading English will eventually get it wrong — and
    the wrong way round is an announcement telling the owner their console was repaired when
    it was not. `settled` is the single question most callers actually have.
    """

    moved: tuple[str, ...]
    blocked: tuple[str, ...]
    settled: bool


@dataclass(frozen=True, slots=True)
class _Unwind:
    """One exchange that moves the console towards rest, and how to describe it."""

    source: str
    target: str
    note: str


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
        # Both of these run *after* the verdict and cannot change it. A console whose surface
        # cannot be marked, or whose arrangement cannot be unwound, is still a console: the
        # tab bar works, sessions open, the binding is installed. Answering "unusable" for a
        # repair that is itself a fallback would take the whole surface down with it.
        #
        # `recover` belongs here and nowhere else on this path: `ensure` is console *start*,
        # which is the one moment the resting arrangement is unambiguously the right one
        # because nothing has been shown yet. Called from the periodic `sync` instead, it
        # would evict an agent the owner is deliberately looking at.
        try:
            await self._adopt_surface()
            report = await self.recover()
            for note in report.moved:
                _LOG.info("console recovery: %s", note)
        except Exception:
            _LOG.exception("the console could not be returned to rest; it may show an agent")
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
            await self._restore_stale_display(live)
        except Exception:
            _LOG.exception("console tab sync failed; tabs may lag until the next pass")

    async def _restore_stale_display(self, live: set[SessionId]) -> None:
        """Bring the surface back when the session it stepped aside for is no longer live.

        **The other writer's half of the stop story (DEC-005).** A local stop asks the console
        to move first (`hide`), but the bot is a different process with no composer and cannot:
        it ends the session and leaves the console displaying the result. Nothing tells the
        console, so the console has to notice, and `sync` already runs on every sessions
        reload — which makes it the pass that notices.

        **What says "not at rest" is where the surface is, not what the slot holds.** The
        obvious rule — "the slot holds a session that has ended" — only catches the graceful
        case, where `remain-on-exit` leaves a dead pane still carrying its mark. A force stop
        removes the pane outright, so tmux shifts a console pane of its own into position 0
        and the slot reads as unremarkable while the surface sits in a window whose session is
        gone. Asking after the surface catches both.

        The refusal is what makes this safe to run on every reload: a slot holding a **live**
        displayed session is left alone. Without it the console would yank itself back to the
        projects list under an owner who was reading an agent.
        """
        arrangement = await self._console.pane_arrangement()
        surface = _surface(arrangement)
        if surface is None or (surface.on_console and surface.window_index == 0):
            return
        slot = _left_slot(arrangement)
        if slot is None or (slot.session_id is not None and slot.session_id in live):
            return
        async with self._links:
            await self._console.swap_panes(surface.pane_id, slot.pane_id)
        _LOG.info("the console was showing a session that has ended; the surface is back")

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

    async def hide(self, session_id: SessionId) -> None:
        """Return the surface to the slot, but only if *this* session is the one shown.

        `show_projects` narrowed to one session, and the narrowing is the whole point: a stop
        must not rearrange the console when it is showing somebody else. Asking for the
        surface unconditionally would yank whatever the owner is looking at back to the
        projects list because an unrelated session happened to end.

        Called before a stop destroys a pane, so the console is never asked to lose a pane
        sitting in its own window. Degrades like everything else here — a console that cannot
        be moved costs the owner the arrangement, never the stop (DEC-006).
        """
        try:
            async with self._links:
                arrangement = await self._console.pane_arrangement()
                slot = _left_slot(arrangement)
                if slot is None or slot.session_id != session_id:
                    return
                await self._send_home(arrangement, slot)
        except Exception:
            _LOG.exception("the console could not be returned to the projects surface")

    async def _send_home(self, arrangement: tuple[HostedPane, ...], slot: HostedPane) -> bool:
        """Exchange the slot's agent with the console's own surface, wherever it is parked."""
        parked = _surface(arrangement)
        if parked is None:
            _LOG.warning(
                "the pane displaced by %s is not identifiable; leaving the console as it is",
                slot.session_id,
            )
            return False
        await self._console.swap_panes(parked.pane_id, slot.pane_id)
        return True

    async def _adopt_surface(self) -> None:
        """Mark the left slot as the console's surface, once, for a console that lacks one.

        One mechanism serving two cases, which is why it lives here rather than in
        `create_console`: a console this composer has just created reaches it with one
        unmarked pane and is marked immediately, and a console already running when the mark
        was introduced gets the same repair on its next start. Narrow, because the wrong guess
        is expensive:

        - **Nothing is marked anywhere** is the precondition. A surface parked in an agent's
          window by an exchange is still a marked surface, so a console merely *showing* an
          agent is left alone. Looking only at the console would find an apparently unmarked
          slot and mark the displaced agent as the surface — after which recovery would swap
          the agent out as though it were the console's own pane.
        - **The slot must hold no identity.** The same protection, checked rather than
          inferred from the first, because the two stop coinciding the moment anything else
          marks a surface.

        A console holding an agent with no marked surface anywhere therefore gets no repair.
        That state is legacy-only, and `recover` reports it rather than guessing at it.
        """
        arrangement = await self._console.pane_arrangement()
        if any(pane.surface for pane in arrangement):
            return
        slot = _left_slot(arrangement)
        if slot is None or slot.session_id is not None:
            return
        await self._console.mark_console_surface(slot.pane_id)

    async def recover(self) -> RecoveryReport:
        """Return the console to its resting arrangement, and say what happened.

        The resting arrangement is the surface in the left slot and every agent in its own
        window. **At console start that is the only correct one** — nothing has been shown
        yet — so anything else is a leftover from a process that died mid-exchange, or from
        tmux used by hand, and neither leaves a record to consult. The arrangement is the
        record. That precondition is the caller's to honour, and nothing in the types enforces
        it: run from a periodic pass instead, this would evict an agent the owner is
        deliberately looking at.

        One exchange per pass, re-reading between them, for the reason `show` re-reads: every
        exchange invalidates the positions the next would be computed from, so a batch decided
        from a single read is right about its first move and guessing about the rest.

        **A problem that cannot be exchanged away never blocks one that can.** A crossed pane
        whose own window does not hold exactly one occupant cannot be unwound — there is
        nothing single to exchange it with — and an earlier version returned that as the
        pass's answer and stopped, leaving a trivially fixable agent in the slot untouched for
        the whole call. It is recorded now, and the pass carries on to what it can fix.

        **The bound is verified, not assumed.** Exhausting the passes is not the same as
        failing: a permutation needing exactly `_RECOVERY_PASSES` exchanges settles on the
        last one, and a loop that only noticed rest at the *top* of the next iteration
        reported that success as a failure. So the bound is followed by one read whose only
        job is to ask whether it settled.

        Presentation throughout (DEC-006, DEC-036): the report is a return value, no record is
        written, and every failure degrades to an empty report.
        """
        moved: list[str] = []
        blocked: tuple[str, ...] = ()
        settled = False
        try:
            async with self._links:
                for _ in range(_RECOVERY_PASSES):
                    step, blocked = _unwind_plan(await self._console.pane_arrangement())
                    if step is None:
                        settled = not blocked
                        break
                    await self._console.swap_panes(step.source, step.target)
                    moved.append(step.note)
                else:
                    step, blocked = _unwind_plan(await self._console.pane_arrangement())
                    settled = step is None and not blocked
                    if not settled:
                        _LOG.warning(
                            "the console did not settle within %d passes", _RECOVERY_PASSES
                        )
                        blocked = (
                            *blocked,
                            f"the console did not settle within {_RECOVERY_PASSES} exchanges; "
                            "some panes are still not where they belong",
                        )
        except Exception:
            _LOG.exception("console recovery failed; the arrangement is left as it was found")
            return RecoveryReport((), (), settled=False)
        for note in blocked:
            _LOG.warning("console recovery could not act: %s", note)
        return RecoveryReport(tuple(moved), blocked, settled=settled)

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


def _surface(arrangement: tuple[HostedPane, ...]) -> HostedPane | None:
    """The console's own projects surface, by its own mark, wherever an exchange left it.

    This replaced "the one unidentified pane in the displaced agent's window", which was an
    inference rather than an answer: an operator's hand-split pane makes two candidates, and
    the composer then refused every exchange forever — the console stuck showing an agent with
    no route back. Marked, the surface is exactly one pane however many sit beside it, and it
    is found even when the agent that displaced it has since ended.

    Still `None` for a console that predates the mark and is caught displaced. Nothing safe
    can be inferred there, and `recover` reports that rather than guessing.
    """
    return next((pane for pane in arrangement if pane.surface), None)


def _crossed_panes(arrangement: tuple[HostedPane, ...]) -> tuple[HostedPane, ...]:
    """Every pane hosted by a *managed* session that is not the one it belongs to.

    The state the composer cannot produce — each exchange it makes has the console's slot on
    one end — but tmux used by hand can, and the one that leaves two sessions answering for
    each other's pane. A console-hosted pane is not crossed: that is the ordinary displayed
    agent, and `_slot_unwind` is what deals with it.
    """
    return tuple(
        pane
        for pane in arrangement
        if pane.session_id is not None and pane.host is not None and pane.host != pane.session_id
    )


def _crossed_unwind(arrangement: tuple[HostedPane, ...]) -> tuple[_Unwind | None, tuple[str, ...]]:
    """The first crossed pane that can be sent home, and every one that cannot.

    A crossed pane goes home by exchanging with whatever occupies its own window. That needs
    the window to hold **exactly one** pane: none means its session is gone, several means
    choosing by listing order, which is the wrong basis Sub-plan 1 removed from destruction.
    Either way there is nothing single to exchange with, so it is reported and the caller
    moves on to what it can fix.
    """
    blocked: list[str] = []
    for pane in _crossed_panes(arrangement):
        occupying = [other for other in arrangement if other.host == pane.session_id]
        if len(occupying) == 1:
            return (
                _Unwind(
                    pane.pane_id,
                    occupying[0].pane_id,
                    f"session {pane.session_id} was hosted by another session's window and "
                    "was returned to its own",
                ),
                tuple(blocked),
            )
        blocked.append(
            f"session {pane.session_id} has a pane in another session's window, and its own "
            f"window holds {len(occupying)} panes rather than one, so it was left where it is"
        )
    return None, tuple(blocked)


def _slot_unwind(arrangement: tuple[HostedPane, ...]) -> tuple[_Unwind | None, tuple[str, ...]]:
    """Bring the projects surface back to the left slot, if an agent is sitting in it."""
    slot = _left_slot(arrangement)
    if slot is None or slot.session_id is None:
        return None, ()
    surface = _surface(arrangement)
    if surface is None:
        return None, (
            f"the console is showing session {slot.session_id} and no pane carries the surface "
            "mark, so the projects surface could not be brought back",
        )
    return (
        _Unwind(
            surface.pane_id,
            slot.pane_id,
            f"session {slot.session_id} was left in the console and was sent home; the "
            "projects surface is back in the left slot",
        ),
        (),
    )


def _unwind_plan(arrangement: tuple[HostedPane, ...]) -> tuple[_Unwind | None, tuple[str, ...]]:
    """The single next exchange to make, plus every problem no exchange can fix.

    One at a time, deliberately: every exchange invalidates the positions the next would be
    computed from. Crossed panes are tried first because they are the pathological state and
    the one a second crash would leave hardest to reason about — but a crossed pane that
    *cannot* be unwound is recorded rather than returned as the answer, so it never blocks a
    slot displacement that is one exchange from fixed.
    """
    step, blocked = _crossed_unwind(arrangement)
    if step is not None:
        return step, blocked
    slot_step, slot_blocked = _slot_unwind(arrangement)
    return slot_step, (*blocked, *slot_blocked)
