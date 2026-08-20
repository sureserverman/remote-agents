"""What a half-swapped console is, and how it gets back to rest.

The composer holds no memory, which is what makes it correct under interruption and also
what makes recovery necessary: after a crash nothing knows what was being shown, so the
arrangement itself has to be read and judged. At console start the resting arrangement is
the only correct one — the projects surface in the left slot, every agent in its own
window — so anything else is something to unwind and report.

**Rest is: the surface is in the left slot and every agent is in its own window.** Everything
else is a state to unwind or to report, and this is the one place they are enumerated. Six,
of which two are recovered, three are reported, and one must produce no exchange at all:

*Recovered:*

- **An agent in the left slot, with the surface parked in that agent's own window.** The
  ordinary "was showing an agent when the process died" state, and the common one. Both panes
  go exactly where they belong, so the exchange costs nothing.
- **A crossed agent** — a pane hosted by a *managed* session that is not its own. The composer
  cannot produce this (every exchange it makes has the console's slot on one end), but tmux by
  hand can, and a pane in a stranger's window is the state nobody would guess from the record.

*Reported, because no exchange can fix them and trying makes things worse:*

- **The surface stranded outside a console that has lost the pane it was traded for.** The
  other writer destroyed the displayed agent's pane. `swap-pane` trades rather than moves, so
  bringing the surface back would exile one of the console's own panes into the defunct
  session — a pane shorter every time. The console needs restarting, and says so.
- **The surface parked in a third session's window while the slot holds an agent.** Exchanging
  would push that agent into a stranger's window: a crossing created by the thing meant to
  remove crossings.
- **No pane carrying the surface mark at all**, on a console that is displaying something. A
  console predating the mark, caught displaced. Nothing distinguishes the parked surface from
  anything else in that window, and guessing could swap a stranger's pane into the console.

*Neither:*

- **The already-correct arrangement**, which must produce no exchange at all. A recovery that
  moves panes on a healthy console is worse than one that does nothing.

A seventh is handled before recovery rather than by it: **a surface mark left behind by a
console that no longer exists.** It outlives its console and would otherwise be deferred to by
the next one; `_adopt_surface` disowns it by noticing that its host window holds no agent.

The surface is found by a mark of its own rather than by being the only unmarked pane in a
window. That distinction is the Stage 1 gate's carried finding: with an operator's hand-split
pane beside it, "the only unmarked one" is not an answer, and the console had no route back.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from remote_agents.application.console import _RECOVERY_PASSES, ConsoleComposer
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.console import HostedPane

_A = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_B = SessionId.parse("11234567-89ab-cdef-0123-456789abcdef")
_C = SessionId.parse("21234567-89ab-cdef-0123-456789abcdef")


def _slot(pane_id: str, identity: SessionId | None = None, *, surface: bool = False) -> HostedPane:
    return HostedPane(None, True, 0, 0, pane_id, identity, surface)


def _feed(pane_id: str) -> HostedPane:
    return HostedPane(None, True, 0, 1, pane_id, None, False)


def _home(
    host: SessionId, pane_id: str, identity: SessionId | None, *, surface: bool = False
) -> HostedPane:
    return HostedPane(host, False, 0, 0, pane_id, identity, surface)


class RecordingConsole:
    """Applies each exchange to its own arrangement, exactly as tmux would."""

    def __init__(self, arrangement: tuple[HostedPane, ...]) -> None:
        self.arrangement = arrangement
        self.swaps: list[tuple[str, str]] = []
        self.marked: list[str] = []

    async def pane_arrangement(self) -> tuple[HostedPane, ...]:
        return self.arrangement

    # `ensure` walks the whole console-creation path before it reaches the surface repair, so
    # the double has to answer it. Written without these, the repair tests passed vacuously:
    # the missing attribute raised, `ensure` swallowed it per DEC-036, and "nothing was
    # marked" looked like the correct answer for the case that should mark.
    async def console_exists(self) -> bool:
        return True

    async def create_console(self, command: tuple[str, ...], cwd: Path) -> None:
        raise AssertionError("an existing console must not be recreated")

    async def install_console_binding(self, key: str, action, command=()) -> None:
        return None

    async def mark_console_slot(self, pane_id: str, slot=None) -> None:
        self.marked.append(pane_id)
        self.arrangement = tuple(
            HostedPane(
                p.host, p.on_console, p.window_index, p.pane_index, p.pane_id, p.session_id, True
            )
            if p.pane_id == pane_id
            else p
            for p in self.arrangement
        )

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
            ),
            target_pane: HostedPane(
                source.host,
                source.on_console,
                source.window_index,
                source.pane_index,
                target.pane_id,
                target.session_id,
                target.surface,
            ),
        }
        self.arrangement = tuple(moved.get(pane.pane_id, pane) for pane in self.arrangement)


def composer(console: RecordingConsole) -> ConsoleComposer:
    return ConsoleComposer(
        console, ("dashboard",), Path("/tmp"), projects_command=("projects",)
    )


def _at_rest(console: RecordingConsole) -> bool:
    """The resting arrangement: the surface in the slot, every agent in its own window."""
    for pane in console.arrangement:
        if pane.on_console and pane.window_index == 0 and pane.pane_index == 0:
            if not pane.surface:
                return False
        if pane.session_id is not None and pane.host != pane.session_id:
            return False
    return True


async def test_an_already_resting_console_is_left_completely_alone() -> None:
    console = RecordingConsole(
        (_slot("%1", surface=True), _feed("%2"), _home(_A, "%3", _A), _home(_B, "%4", _B))
    )

    report = await composer(console).recover()

    assert console.swaps == []
    assert (report.moved, report.blocked, report.settled) == ((), (), True)


async def test_an_agent_left_in_the_slot_is_sent_home_and_the_surface_returns() -> None:
    """The ordinary crash state: the console died while showing an agent."""
    console = RecordingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True), _home(_B, "%4", _B))
    )

    report = await composer(console).recover()

    assert console.swaps == [("%1", "%3")]
    assert _at_rest(console)
    assert report.settled and report.blocked == ()
    assert len(report.moved) == 1 and str(_A) in report.moved[0]


async def test_the_surface_is_found_by_its_mark_not_by_being_the_only_unmarked_pane() -> None:
    """The Stage 1 gate's carried finding, as the case that used to strand the console.

    An operator's hand-split pane sits in the agent's own window beside the parked surface.
    Identified by absence of a mark, neither one is the answer and recovery refuses forever;
    identified by its own mark, the surface is unambiguous however many panes are beside it.
    """
    console = RecordingConsole(
        (
            _slot("%3", _A),
            _feed("%2"),
            _home(_A, "%1", None, surface=True),
            HostedPane(_A, False, 0, 1, "%9", None, False),
        )
    )

    report = await composer(console).recover()

    assert console.swaps == [("%1", "%3")]
    assert _at_rest(console)
    assert report.settled and len(report.moved) == 1


async def test_two_crossed_agents_are_each_returned_to_their_own_window() -> None:
    """A pane hosted by a managed session that is not its own.

    The composer cannot produce this — every exchange it makes has the console's slot on one
    end — but tmux by hand can, and it is the state that would otherwise leave two sessions
    each answering for the other's pane.
    """
    console = RecordingConsole(
        (_slot("%1", surface=True), _feed("%2"), _home(_A, "%4", _B), _home(_B, "%3", _A))
    )

    report = await composer(console).recover()

    assert _at_rest(console), f"crossed agents were not unwound: {console.arrangement}"
    assert report.settled and report.blocked == ()
    assert len(report.moved) >= 1


async def test_a_crossed_agent_is_unwound_before_the_surface_is_brought_back() -> None:
    """Both states at once, which is the only arrangement that can prove the ordering.

    **This test proved nothing until Task 2.1's review.** It built one arrangement, then
    overwrote `console.arrangement` before calling anything — so the arrangement it described
    was dead code — and in *both* versions the pane meant to be crossed had `host ==
    session_id`, which is a pane at home. It quietly degenerated into the same case as the
    agent-in-slot test above and passed for that reason.

    Written properly, the console shows agent A in its slot with the surface parked in A's
    window, *and* agents B and C sit in each other's windows. The crossed pair is genuinely
    crossed — `host != session_id` on both — and the assertion is on the **order** of the
    exchanges rather than only the end state, because an end-state assertion passes for a
    recovery that got there in any order at all.
    """
    console = RecordingConsole(
        (
            _slot("%3", _A),
            _feed("%2"),
            _home(_A, "%1", None, surface=True),
            _home(_B, "%5", _C),
            _home(_C, "%4", _B),
        )
    )

    report = await composer(console).recover()

    assert _at_rest(console), f"the console did not settle: {console.arrangement}"
    assert report.settled and report.blocked == ()
    assert len(console.swaps) == 2, console.swaps
    assert console.swaps[-1] == ("%1", "%3"), (
        f"the surface was brought back before the crossed pair was unwound: {console.swaps}"
    )
    assert "another session's window" in report.moved[0]
    assert "left in the console" in report.moved[1]


async def test_a_crossed_pane_that_cannot_be_unwound_does_not_block_the_surface() -> None:
    """The starvation the ordering used to cause, which is why "first" is not "only".

    Agent B's pane sits in C's window, and B's own window holds two panes — so there is
    nothing single to exchange B with and no safe unwind. Meanwhile agent A is in the console
    slot with its surface parked and correctly marked: one exchange from fixed. An earlier
    version returned the unresolvable crossed pane as the pass's answer and stopped, so the
    fixable problem went untouched for the whole call. Now the blocked case is recorded and
    the pass carries on.
    """
    console = RecordingConsole(
        (
            _slot("%3", _A),
            _feed("%2"),
            _home(_A, "%1", None, surface=True),
            _home(_C, "%5", _B),
            _home(_B, "%6", None),
            HostedPane(_B, False, 0, 1, "%7", None, False),
        )
    )

    report = await composer(console).recover()

    assert console.swaps == [("%1", "%3")], (
        "an unresolvable crossed pane starved the fixable slot displacement"
    )
    assert len(report.moved) == 1 and "left in the console" in report.moved[0]
    assert len(report.blocked) == 1 and str(_B) in report.blocked[0]
    assert not report.settled, "a blocked problem remains, so the console is not at rest"


async def test_a_console_with_no_marked_surface_reports_rather_than_guessing() -> None:
    """A console created before the mark existed, found mid-swap.

    There is no safe guess: the pane parked in the agent's window might be the surface or
    might be something an operator put there. Reporting leaves the owner a console that
    plainly shows an agent, which is visible; guessing could swap a stranger's pane into the
    console and lose the surface entirely.
    """
    console = RecordingConsole((_slot("%3", _A), _feed("%2"), _home(_A, "%1", None)))

    report = await composer(console).recover()

    assert console.swaps == []
    assert report.moved == (), "nothing was moved, so nothing may be reported as moved"
    assert len(report.blocked) == 1 and "surface" in report.blocked[0].lower()
    assert not report.settled


async def test_recovery_reports_every_move_it_made_and_never_writes_a_record() -> None:
    """Presentation, like the rest of the composer (DEC-006/DEC-036).

    The report is the return value — the caller decides whether the owner sees it. Recovery
    moving panes must never become something a session's state depends on.
    """
    console = RecordingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True))
    )

    report = await composer(console).recover()

    assert all(isinstance(line, str) for line in (*report.moved, *report.blocked))
    assert report.moved, "a recovery that moved a pane must say so"


async def test_a_broken_console_degrades_to_nothing_rather_than_raising() -> None:
    class Broken(RecordingConsole):
        async def pane_arrangement(self):
            raise RuntimeError("no server running on /tmp/tmux-1000/remote-agents")

    report = await composer(Broken(())).recover()

    assert (report.moved, report.blocked, report.settled) == ((), (), False)


async def test_settling_marks_the_left_slot_as_the_surface_when_nothing_carries_the_mark(
) -> None:
    """The repair path for a console that predates the mark, and its precondition.

    On `settle` rather than `ensure`, deliberately: `ensure` is called by anything that needs
    the console to exist, including a second terminal re-entering one that is already running,
    which is not a start. The repair marks the left slot only when it holds no agent — marking
    a displaced agent as the surface would make recovery swap the agent out as though it were
    the console's own pane.
    """
    console = RecordingConsole((_slot("%1"), _feed("%2"), _home(_A, "%3", _A)))

    await composer(console).settle()

    assert console.marked == ["%1"]


async def test_settling_never_marks_a_pane_that_carries_an_agents_identity() -> None:
    console = RecordingConsole((_slot("%3", _A), _feed("%2"), _home(_A, "%1", None)))

    await composer(console).settle()

    assert console.marked == []


async def test_settling_marks_nothing_when_a_surface_pane_already_exists_anywhere() -> None:
    """Including one parked out of the console by an exchange — which is the whole point.

    A console showing an agent has its surface in that agent's window. If `ensure` looked
    only at the console, it would find an unmarked-by-its-reckoning slot and mark the agent.
    """
    console = RecordingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True))
    )

    await composer(console).settle()

    assert console.marked == []


def _crossed_ring(count: int) -> tuple[HostedPane, ...]:
    """`count` agents, each parked in the next one's window — one exchange short of a ring.

    A cycle of length n needs n-1 exchanges to unwind: each one puts a pane home for good, and
    the last exchange settles the final two together. Built as a real permutation rather than
    by asserting a number, so the count the test relies on is a property of the arrangement.
    """
    agents = [
        SessionId.parse(f"{index:08x}-0000-0000-0000-000000000001") for index in range(count)
    ]
    return (
        _slot("%0", surface=True),
        _feed("%1"),
        *(
            _home(agents[index], f"%{index + 2}", agents[(index + 1) % count])
            for index in range(count)
        ),
    )


async def test_a_permutation_that_settles_on_the_last_permitted_exchange_reports_success() -> None:
    """Exhausting the bound is not the same as failing, and the difference is one read.

    A ring of `_RECOVERY_PASSES + 1` agents needs exactly `_RECOVERY_PASSES` exchanges. The
    loop performs the last one and then has no iteration left in which to notice that it
    worked — so the version that recognised rest only at the *top* of the next pass reported
    this exact case as "did not settle", which is a false failure in the one place a caller
    would most trust the answer. A verify-only read after the bound is what tells them apart.
    """
    console = RecordingConsole(_crossed_ring(_RECOVERY_PASSES + 1))

    report = await composer(console).recover()

    assert len(console.swaps) == _RECOVERY_PASSES, console.swaps
    assert _at_rest(console)
    assert report.settled, f"a recovery that settled reported failure: {report.blocked}"
    assert report.blocked == ()


async def test_a_permutation_too_large_for_the_bound_says_so_rather_than_looping() -> None:
    """And the other side of the boundary, so "settled" is not simply always true."""
    console = RecordingConsole(_crossed_ring(_RECOVERY_PASSES + 3))

    report = await composer(console).recover()

    assert len(console.swaps) == _RECOVERY_PASSES
    assert not report.settled
    assert any("did not settle" in note for note in report.blocked), report.blocked


class _SyncingConsole(RecordingConsole):
    """A recording console that also answers the tab half of `sync`."""






def _record(session_id: SessionId, state: SessionState) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


async def test_the_other_writer_ending_a_shown_session_restores_the_surface_on_the_next_sync(
) -> None:
    """DEC-005's two-writer story, at the one place the swap model makes it visible.

    The bot is a different process with no composer, so it cannot ask the console to step
    aside the way a local stop does (Task 2.2). It stops the session, the pane it leaves in
    the console's slot is dead, and the projects surface is still parked in a window whose
    session has ended. Nothing tells the console — so the console has to notice, and `sync`
    is the pass that already runs on every sessions reload.
    """
    console = _SyncingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True))
    )

    await composer(console).sync((_record(_A, SessionState.ENDED),))

    assert console.swaps == [("%1", "%3")], (
        "a session ended by the other writer left its dead pane in the console's slot"
    )


async def test_the_other_writer_leaves_a_live_shown_session_alone_on_sync() -> None:
    """The refusal that makes the rule safe to run on every reload.

    `sync` fires constantly. If it restored the surface whenever an agent occupied the slot,
    the owner could never look at an agent for longer than one refresh — the console would
    yank itself back to the projects list under them.
    """
    console = _SyncingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True))
    )

    await composer(console).sync((_record(_A, SessionState.RUNNING),))

    assert console.swaps == []


async def test_a_sync_on_a_resting_console_moves_nothing_for_the_other_writer() -> None:
    console = _SyncingConsole((_slot("%1", surface=True), _feed("%2"), _home(_A, "%3", _A)))

    await composer(console).sync((_record(_A, SessionState.RUNNING),))

    assert console.swaps == []


async def test_the_other_writer_restore_cannot_act_on_an_arrangement_that_has_since_moved(
) -> None:
    """The restore decides and acts under one lock hold, like every other swap here.

    Deciding outside it and swapping inside is a stale-read: `sync` reads "session A is in the
    slot and has ended, the surface is parked in A's window", and before it reacquires the
    lock a `show(B)` runs to completion — sending A home and bringing B in. The two pane ids
    `sync` is still holding now name entirely different places, and swapping them blindly puts
    **A's live pane into B's own window**: a crossed pane, hosted by a session that is not its
    own, with nothing raised. From there a later stop of B destroys B's window and takes A's
    agent with it, because tmux drops a window's session with its last pane.

    Dormant only because `show` has no production caller until Sub-plan 3 — which is the
    sub-plan that wires it. Asserted here as the invariant rather than the symptom: whatever
    the interleaving, no pane ends up hosted by a session that is not its own.
    """
    console = _SyncingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True), _home(_B, "%4", _B))
    )
    gate = asyncio.Event()
    original = console.pane_arrangement
    first = True

    async def gated_read() -> tuple[HostedPane, ...]:
        """Snapshot first, *then* wait — which is what a stale read actually is.

        Waiting and then reading returns fresh state and races nothing; the first draft of
        this test did that and passed against the unfixed code, which is the tell.
        """
        nonlocal first
        if first:
            first = False
            snapshot = await original()
            await gate.wait()
            return snapshot
        return await original()

    one = composer(console)
    console.pane_arrangement = gated_read  # type: ignore[method-assign]

    syncing = asyncio.create_task(one.sync((_record(_B, SessionState.RUNNING),)))
    await asyncio.sleep(0)
    showing = asyncio.create_task(one.show(_B))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(syncing, showing)

    crossed = [
        pane
        for pane in console.arrangement
        if pane.session_id is not None and pane.host is not None and pane.host != pane.session_id
    ]
    assert crossed == [], f"a stale restore crossed a live agent into another session: {crossed}"


async def test_making_the_console_exist_never_runs_the_start_only_repair() -> None:
    """`ensure` and `settle` are split because only one of their callers is a start.

    A second terminal running the bare command re-enters a console that may already be
    displaying an agent for somebody in another terminal. It calls `ensure` — and if `ensure`
    also recovered, that re-entry would evict the agent out from under them, reported as a log
    line. Asserted as the absence: making the console exist moves nothing and marks nothing.
    """
    console = RecordingConsole((_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True)))

    assert await composer(console).ensure() is True
    assert (console.swaps, console.marked) == ([], [])


def _orphaned(host: SessionId, pane_id: str) -> HostedPane:
    """A surface mark stranded in a window whose session holds no agent at all."""
    return HostedPane(host, False, 0, 0, pane_id, None, True)


async def test_a_surface_stranded_outside_the_console_is_never_reported_as_rest() -> None:
    """Rest is "the surface is in the slot", not "no agent is in the slot".

    The two coincide until the displayed agent's pane is *destroyed* rather than moved — the
    other writer's force stop. Then tmux shifts one of the console's own panes into position
    0, the slot holds no identity, and a rule written about the slot's occupant sees nothing
    wrong while the projects surface sits in a defunct session's window. `settled=True` over
    that is worse than silence, because `settled` is the one field a caller trusts.

    This module's own `_restore_stale_display` docstring already said so — "what says 'not at
    rest' is where the surface is, not what the slot holds" — while `_slot_unwind` implemented
    the rule that sentence rejects. The oracle in this file (`_at_rest`) encoded the correct
    rule the whole time; no test made the two disagree, so the gap was invisible.
    """
    console = RecordingConsole(
        (
            HostedPane(None, True, 0, 0, "%2", None, False),
            HostedPane(None, True, 0, 1, "%1", None, False),
            _orphaned(_A, "%0"),
        )
    )

    report = await composer(console).recover()

    assert not report.settled, "a console with its surface stranded outside reported rest"
    assert report.blocked, "and said nothing about it"


async def test_a_stranded_surface_is_reported_rather_than_bought_back_by_exiling_a_pane() -> None:
    """The exchange that would "fix" it costs the console another pane, every time.

    `swap-pane` exchanges; it cannot move a pane home on its own. With the displaced agent's
    pane destroyed, the console's slot holds one of the console's *own* panes, so swapping the
    surface in sends that pane out into the dead session's window — the console is one pane
    shorter, the defunct session is kept alive holding it, and repeating the sequence shaves
    the console again. The honest answer is to say the console is short a pane.

    **This replaces a Task 2.3 test that asserted the exile as the desired behaviour** —
    `test_the_other_writer_killing_the_pane_outright_still_brings_the_surface_back`, named for
    an outcome that is real but costs a console pane every time it happens. The gate evaluator
    traced where those panes go. Bringing the surface back is not worth quietly dismantling
    the console to do it.
    """
    console = _SyncingConsole(
        (
            HostedPane(None, True, 0, 0, "%2", None, False),
            HostedPane(None, True, 0, 1, "%1", None, False),
            _orphaned(_A, "%0"),
        )
    )

    await composer(console).sync(())

    assert console.swaps == [], "the restore exiled a console pane into a dead session"


async def test_an_agent_left_in_the_slot_is_still_exchanged_because_both_panes_go_home(
) -> None:
    """The distinction that keeps the rule above from refusing the case it must handle.

    When the slot holds an agent and the surface is parked in *that agent's own window*, the
    exchange sends each pane exactly where it belongs. Nothing is exiled, so this one is
    always safe — and it is the ordinary crash state.
    """
    console = RecordingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True))
    )

    report = await composer(console).recover()

    assert console.swaps == [("%1", "%3")]
    assert report.settled and _at_rest(console)


async def test_a_surface_parked_in_a_third_sessions_window_is_not_exchanged_into_a_crossing(
) -> None:
    """Exchanging would send the slot's agent into a window belonging to somebody else.

    The recovery loop would unwind that crossing on a later pass, so the end state converges —
    but creating a crossed pane in order to fix a displacement is the recovery making the
    arrangement worse before it makes it better, and a second crash in the gap leaves it worse.
    """
    console = RecordingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_B, "%1", None, surface=True), _home(_A, "%4", None))
    )

    report = await composer(console).recover()

    assert console.swaps == []
    assert not report.settled and report.blocked


async def test_a_new_console_disowns_a_surface_mark_left_by_one_that_was_destroyed() -> None:
    """The mark outlives the console that made it, and a fresh console must not defer to it.

    Killing a console while an agent was displayed leaves the old surface pane in that agent's
    window — and that pane is what keeps the defunct session alive. A new console starts with
    an unmarked slot, and `_adopt_surface`'s "something is already marked, leave it alone"
    precondition cannot tell that stranded mark from its own surface parked during a live
    display. Deferring to it, the new console never marks anything, and `show_projects` later
    swaps that stranger's pane in while exiling a live agent into the defunct session.

    Told apart by what the host window holds: a surface parked during a live display sits in a
    window that still has its agent; an orphan's host has no managed pane at all.
    """
    console = RecordingConsole(
        (_slot("%5"), _feed("%6"), _orphaned(_A, "%0"))
    )

    report = await composer(console).settle()

    assert console.marked == ["%5"], "the new console deferred to a dead console's mark"
    # Disowning is not cleaning up: the old surface process is still running and is the only
    # thing keeping its host session alive. Reported once, because the previous start's honest
    # "the console is a pane short" was otherwise followed by silence forever after.
    assert any(str(_A) in note for note in report.blocked), report.blocked
    assert report.settled, "the console's own arrangement is at rest; the leak is not about it"


async def test_a_surface_parked_during_a_live_display_is_still_left_alone() -> None:
    """The other side of that test, so "disown an orphan" cannot become "disown anything"."""
    console = RecordingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True))
    )

    await composer(console).settle()

    assert console.marked == []


async def test_a_console_whose_window_is_not_index_zero_is_still_found() -> None:
    """`set -g base-index 1` is common, and the server reads the owner's `~/.tmux.conf`.

    Hardcoded to window 0, `_left_slot` found no console panes at all — and every caller read
    that as rest. On such a host the surface was never marked, `show` silently did nothing,
    and `recover` answered `settled=True` unconditionally, including over a console somebody
    had displaced by hand. A state that is merely unlikely still has to be named; this one was
    not even reachable.
    """
    console = RecordingConsole(
        (
            HostedPane(None, True, 1, 0, "%1", None, True),
            HostedPane(None, True, 1, 1, "%2", None, False),
            HostedPane(_A, False, 1, 0, "%3", _A, False),
        )
    )

    report = await composer(console).recover()

    assert report.settled and console.swaps == []

    displaced = RecordingConsole(
        (
            HostedPane(None, True, 1, 0, "%3", _A, False),
            HostedPane(None, True, 1, 1, "%2", None, False),
            HostedPane(_A, False, 1, 0, "%1", None, True),
        )
    )

    moved = await composer(displaced).recover()

    assert displaced.swaps == [("%1", "%3")], "a console at base-index 1 could not be recovered"
    assert moved.settled


async def test_a_surface_out_of_position_inside_the_console_is_put_back_at_any_base_index() -> None:
    """The third shape `_slot_unwind` enumerates, which had no test at either window index.

    Both panes belong to the console; they are simply the wrong way round, so reordering them
    exiles nothing and is always safe. Written against a literal window 0, the branch sent a
    console at `set -g base-index 1` down the report-and-restart path instead — telling the
    operator a pane had been destroyed when none had.
    """
    for window in (0, 1):
        console = RecordingConsole(
            (
                HostedPane(None, True, window, 0, "%2", None, False),
                HostedPane(None, True, window, 1, "%1", None, True),
            )
        )

        report = await composer(console).recover()

        assert console.swaps == [("%1", "%2")], f"base-index {window}: {console.swaps}"
        assert report.settled and report.blocked == (), f"base-index {window}: {report}"
        assert "out of position" in report.moved[0]


async def test_an_agent_mis_parked_elsewhere_in_the_console_is_not_reported_as_rest() -> None:
    """A pane can be in the wrong place inside the console, not only in the slot.

    `_crossed_panes` looks for a pane hosted by a *managed* session that is not its own, and a
    console-hosted row has no host at all — so an agent swapped into one of the console's
    **other** panes was invisible to it, while `_slot_unwind` only ever inspects the slot.
    Between them they answered rest over a console holding somebody's agent in its feed
    position, with one of the console's own panes exiled into that agent's window.

    Reachable the same way the managed-to-managed crossing is: by hand, with `swap-pane`
    against a console pane that is not the slot. The composer cannot produce it — every
    exchange it makes has the slot on one end — which is exactly the argument that was made
    for the crossing this function already covers.

    Recovered rather than merely reported, which is one better than the finding asked for:
    the agent's own window holds the console pane it was exchanged with, so trading them back
    puts each where it belongs and exiles nothing.
    """
    console = RecordingConsole(
        (
            _slot("%0", surface=True),
            HostedPane(None, True, 0, 1, "%2", _A, False),
            _home(_A, "%1", None),
        )
    )

    report = await composer(console).recover()

    assert console.swaps == [("%2", "%1")], console.swaps
    assert _at_rest(console), "the mis-parked agent was not returned to its own window"
    assert report.settled and len(report.moved) == 1, report
    assert str(_A) in report.moved[0]


async def test_sending_an_agent_home_never_puts_it_in_a_third_sessions_window() -> None:
    """`_send_home` must refuse exactly what `_slot_unwind` refuses, for the same reason.

    It swapped the slot against the surface wherever the surface happened to be. With the
    surface parked in a *third* session's window — the state `_slot_unwind` reports as blocked
    rather than exchanging — that put the displayed agent's live pane into that third session's
    window. `show`, `show_projects` and `hide` all route through it, and `hide` is wired into
    every stop path, so the refusal has to live in the shared method rather than only in the
    caller that already knew.
    """
    console = RecordingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_B, "%1", None, surface=True), _home(_A, "%4", None))
    )

    await composer(console).show_projects()

    assert console.swaps == [], "the surface was traded from a third session's window"
    crossed = [
        pane
        for pane in console.arrangement
        if pane.session_id is not None and pane.host is not None and pane.host != pane.session_id
    ]
    assert crossed == []


async def test_only_the_process_in_the_left_slot_may_settle_the_console() -> None:
    """"Hosted by the console" is true of every pane on this server, which is not the same
    thing as being the console's surface.

    `hosting_mode` decides by socket name, so a second console pane, an operator's hand-split,
    or an agent's own pane all report `CONSOLE` — and any of them running the dashboard would
    have called the start-only repair, evicting an agent the owner is reading in the real
    console. The process knows which pane it is; the check uses that rather than the socket.
    """
    console = RecordingConsole((_slot("%1", surface=True), _feed("%2"), _home(_A, "%3", _A)))

    refused = await composer(console).settle("%2")

    assert (refused.moved, refused.blocked, refused.settled) == ((), (), False)
    assert console.marked == [], "a process outside the left slot repaired the console"

    allowed = await composer(console).settle("%1")

    assert allowed.settled, "the process in the left slot was refused"
