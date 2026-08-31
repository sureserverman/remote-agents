"""A console that lost a pane to a stopped session gets it back, and never loses a second.

The defect these cover was reported from the owner's own console on 2026-08-21 and read off
the live tmux server rather than guessed at. The sequence, in the order it happened:

1. The console showed a session. Under the swap model that means the agent's pane is *in* the
   console and one of the console's own panes — the projects surface — is parked in the
   agent's window.
2. That session was stopped and cleaned up. `TmuxTerminal.cleanup` kills the agent's **pane**,
   and by then the agent's pane is the one sitting in the console. tmux closed the gap, and
   the console was down to two panes with its surface stranded in a window with no agent left
   in it. The session's own tmux session survived precisely because the console's pane was in
   it.
3. Nothing put it back. `_slot_unwind` recognised the state and reported it — at `debug`, from
   `sync`, on a process that configures no logging — so what the owner saw was the left pane
   silently gone.
4. The owner then clicked a row in the sessions list. `_left_slot` is a *position*, and the
   pane tmux had shifted into position 0 was the **sessions** pane, so `show` exchanged the
   agent into it and exiled the sessions list into that agent's window. The console was down
   to two panes again, with the agent showing across the top.

So there are two rules here, and they are separate: a console short of a pane puts it back
(`_reclaim_plan`), and a console that cannot must refuse to trade another one away (`show`).
The second is what stops the damage compounding while the first is unavailable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from remote_agents.application.console import ConsoleComposer
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.console import ConsolePaneSlot, HostedPane

_A = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_B = SessionId.parse("11234567-89ab-cdef-0123-456789abcdef")

_PROJECTS = ConsolePaneSlot.PROJECTS.value
_SESSIONS = ConsolePaneSlot.SESSIONS.value
_FEED = ConsolePaneSlot.FEED.value


def _console_pane(pane_id: str, index: int, slot: str | None, identity=None) -> HostedPane:
    return HostedPane(None, True, 0, index, pane_id, identity, slot == _PROJECTS, slot)


def _parked(host: SessionId, pane_id: str, slot: str) -> HostedPane:
    """One of the console's panes, sitting in a managed session's window after an exchange."""
    return HostedPane(host, False, 0, 0, pane_id, None, slot == _PROJECTS, slot)


def _agent(host: SessionId, pane_id: str) -> HostedPane:
    return HostedPane(host, False, 0, 0, pane_id, host, False, None)


class RecordingConsole:
    """Applies each move to its own arrangement the way tmux does, reindexing as it goes."""

    def __init__(self, arrangement: tuple[HostedPane, ...]) -> None:
        self.arrangement = arrangement
        self.swaps: list[tuple[str, str]] = []
        self.rejoined: list[tuple[str, str, bool, int, bool]] = []
        self.normalized: list[tuple[int, tuple[tuple[str, int], ...]]] = []

    async def pane_arrangement(self) -> tuple[HostedPane, ...]:
        return self.arrangement

    async def console_exists(self) -> bool:
        return True

    async def create_console(self, command: tuple[str, ...], cwd: Path) -> None:
        raise AssertionError("an existing console must not be recreated")

    async def install_console_binding(self, key: str, action, command=()) -> None:
        return None

    async def mark_console_slot(self, pane_id: str, slot=None) -> None:
        raise AssertionError("nothing here re-marks a pane")

    async def swap_panes(self, source_pane: str, target_pane: str) -> None:
        self.swaps.append((source_pane, target_pane))
        by_id = {pane.pane_id: pane for pane in self.arrangement}
        source, target = by_id[source_pane], by_id[target_pane]
        moved = {
            source_pane: HostedPane(
                target.host,
                target.on_console,
                target.window_index,
                target.pane_index,
                source.pane_id,
                source.session_id,
                source.surface,
                source.console_slot,
            ),
            target_pane: HostedPane(
                source.host,
                source.on_console,
                source.window_index,
                source.pane_index,
                target.pane_id,
                target.session_id,
                target.surface,
                target.console_slot,
            ),
        }
        self.arrangement = tuple(moved.get(pane.pane_id, pane) for pane in self.arrangement)

    async def rejoin_console_pane(
        self, pane_id, beside_pane, *, vertical: bool, percent: int, before: bool = False
    ) -> None:
        """Move the pane onto the console beside another, and renumber what is there.

        The renumbering is the part worth modelling rather than stubbing: `_left_slot` is a
        *position*, so a double that moved the pane without giving it position 0 would let
        every assertion below pass over a repair that had not happened.
        """
        self.rejoined.append((pane_id, beside_pane, vertical, percent, before))
        order = [pane.pane_id for pane in self.arrangement if pane.on_console]
        at = order.index(beside_pane)
        order.insert(at if before else at + 1, pane_id)
        placed = {pane_id: order.index(pane_id) for pane_id in order}
        self.arrangement = tuple(
            HostedPane(
                None,
                True,
                0,
                placed[pane.pane_id],
                pane.pane_id,
                pane.session_id,
                pane.surface,
                pane.console_slot,
            )
            if pane.pane_id in placed
            else pane
            for pane in self.arrangement
        )

    async def normalize_console_layout(self, main_percent: int, column) -> None:
        self.normalized.append((main_percent, tuple(column)))


def _composer(console: RecordingConsole) -> ConsoleComposer:
    return ConsoleComposer(console, ("dashboard",), Path("/tmp"), projects_command=("projects",))


def _record(session_id: SessionId, state: SessionState) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque", "claude", "regular", 1),
        state,
        datetime(2026, 8, 21, 6, 25, tzinfo=UTC),
    )


def _stopped_while_displayed() -> RecordingConsole:
    """The reported state: the console is two panes, its surface stranded, the agent gone.

    Read off the live server on 2026-08-21 — `@remote_agents_console_slot surface` on a pane
    in a managed session's window, that session holding no agent pane at all, and the console
    window holding only the sessions list and the feed.
    """
    return RecordingConsole(
        (
            _console_pane("%2", 0, _SESSIONS),
            _console_pane("%3", 1, _FEED),
            _parked(_A, "%1", _PROJECTS),
        )
    )


def _healthy() -> RecordingConsole:
    return RecordingConsole(
        (
            _console_pane("%1", 0, _PROJECTS),
            _console_pane("%2", 1, _SESSIONS),
            _console_pane("%3", 2, _FEED),
            _agent(_B, "%4"),
        )
    )


async def test_the_surface_comes_back_when_the_session_showing_it_was_stopped() -> None:
    """The repair, on the pass that already notices what the other writer did.

    A swap cannot do this: there is nothing in the stopped session's window to trade for, so
    trading would send the sessions pane out to replace the surface. A move takes one pane and
    no partner.
    """
    console = _stopped_while_displayed()

    await _composer(console).sync((_record(_A, SessionState.ENDED),))

    assert console.rejoined == [("%1", "%2", False, 60, True)]
    assert console.swaps == [], "a pane was traded away to bring the surface back"
    slots = [
        (pane.pane_index, pane.console_slot) for pane in console.arrangement if pane.on_console
    ]
    assert sorted(slots) == [(0, _PROJECTS), (1, _SESSIONS), (2, _FEED)]


async def test_the_reclaimed_console_is_put_back_in_its_declared_proportions() -> None:
    """A rejoined pane lands in the right order and the wrong shape — measured on tmux 3.4,
    the feed ran the full width under both. Same defect a rebuilt pane has, same repair."""
    console = _stopped_while_displayed()

    await _composer(console).sync((_record(_A, SessionState.ENDED),))

    assert console.normalized == [(60, (("%2", 46), ("%3", 33)))]


async def test_a_session_that_is_still_being_shown_is_left_completely_alone() -> None:
    """The condition that separates the two states, and the one an edit is likeliest to drop.

    While the agent is alive its pane is in the console and the surface is parked in *its*
    window. That is an ordinary display, and the exchange that made it is what must undo it —
    reclaiming there would pull the surface back under an owner who is reading an agent.
    """
    console = RecordingConsole(
        (
            _console_pane("%4", 0, None, identity=_A),
            _console_pane("%2", 1, _SESSIONS),
            _console_pane("%3", 2, _FEED),
            _parked(_A, "%1", _PROJECTS),
        )
    )

    await _composer(console).sync((_record(_A, SessionState.RUNNING),))

    assert console.rejoined == []
    assert console.swaps == []


async def test_showing_a_session_repairs_the_console_on_the_way() -> None:
    """The click the owner actually made. It found the console a pane short, and under the
    old code it paid for that by exiling a second one; it now puts the first one back."""
    console = _stopped_while_displayed()
    console.arrangement = (*console.arrangement, _agent(_B, "%4"))

    refused = await _composer(console).show(_B)

    assert refused is None
    assert console.rejoined == [("%1", "%2", False, 60, True)]
    # The surface went back to the slot, and *then* the agent was exchanged with it — so the
    # console still has three panes and the sessions list is still one of them.
    assert console.swaps == [("%4", "%1")]
    on_console = {pane.console_slot for pane in console.arrangement if pane.on_console}
    assert on_console == {None, _SESSIONS, _FEED}


async def test_a_console_that_cannot_be_repaired_refuses_to_trade_another_pane_away() -> None:
    """The second defect, on its own. Here the surface is not merely parked — it is gone, so
    there is nothing to reclaim — and the pane tmux shifted into position 0 is the sessions
    list. Exchanging into it is what cost the owner their second pane."""
    console = RecordingConsole(
        (
            _console_pane("%2", 0, _SESSIONS),
            _console_pane("%3", 1, _FEED),
            _agent(_B, "%4"),
        )
    )

    refused = await _composer(console).show(_B)

    assert refused is not None and "pane short" in refused
    assert console.swaps == [], "the sessions pane was exiled into the agent's window"
    assert console.rejoined == []


async def test_an_unmarked_left_slot_may_still_be_exchanged() -> None:
    """A console predating the slot marks has an unmarked surface, and refusing that would
    take the feature away from it to prevent damage it cannot suffer."""
    console = RecordingConsole(
        (
            _console_pane("%1", 0, None),
            _console_pane("%3", 1, None),
            _agent(_B, "%4"),
        )
    )

    refused = await _composer(console).show(_B)

    assert refused is None
    assert console.swaps == [("%4", "%1")]


async def test_recovery_reclaims_at_console_start_and_reports_it() -> None:
    """The same state met at a start rather than mid-session: it is repaired, and said."""
    console = _stopped_while_displayed()

    report = await _composer(console).recover()

    assert console.rejoined == [("%1", "%2", False, 60, True)]
    assert report.settled and report.blocked == ()
    assert len(report.moved) == 1 and str(_A) in report.moved[0]


async def test_a_healthy_console_is_never_touched() -> None:
    """The rule every repair here has to obey: moving panes on a well console is worse than
    doing nothing at all."""
    console = _healthy()

    report = await _composer(console).recover()
    await _composer(console).sync((_record(_B, SessionState.RUNNING),))

    assert console.rejoined == [] and console.swaps == [] and console.normalized == []
    assert report.settled and report.moved == () and report.blocked == ()
