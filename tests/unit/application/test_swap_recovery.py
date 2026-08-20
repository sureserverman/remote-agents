"""What a half-swapped console is, and how it gets back to rest.

The composer holds no memory, which is what makes it correct under interruption and also
what makes recovery necessary: after a crash nothing knows what was being shown, so the
arrangement itself has to be read and judged. At console start the resting arrangement is
the only correct one — the projects surface in the left slot, every agent in its own
window — so anything else is something to unwind and report.

Three states can be found there, and the fourth case is the one that must move nothing:

- **An agent in the left slot** with the surface parked in that agent's window. The ordinary
  "was showing an agent when the process died" state, and the common one.
- **A crossed agent** — a pane hosted by a *managed* session that is not its own. The
  composer cannot produce this (every exchange it makes has the console's slot on one end),
  but tmux by hand can, and a pane in a stranger's window is the state nobody would guess
  from the record.
- **The already-correct arrangement**, which must produce no exchange at all. A recovery that
  moves panes on a healthy console is worse than one that does nothing.

The surface is found by a mark of its own rather than by being the only unmarked pane in a
window. That distinction is the Stage 1 gate's carried finding: with an operator's hand-split
pane beside it, "the only unmarked one" is not an answer, and the console had no route back.
"""

from __future__ import annotations

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

    async def install_console_binding(self, key: str) -> None:
        return None

    async def mark_console_surface(self, pane_id: str) -> None:
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
    return ConsoleComposer(console, ("dashboard",), Path("/tmp"))


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


async def test_ensure_marks_the_left_slot_as_the_surface_when_nothing_carries_the_mark() -> None:
    """The repair path for a console that predates the mark, and its precondition.

    Marked at creation the question never arises; a console already running when this shipped
    has no marked pane, and `ensure` runs on every start. It marks the left slot only when it
    holds no agent — marking a displaced agent as the surface would make recovery swap the
    agent out as though it were the console's own pane.
    """
    console = RecordingConsole((_slot("%1"), _feed("%2"), _home(_A, "%3", _A)))

    await composer(console).ensure()

    assert console.marked == ["%1"]


async def test_ensure_never_marks_a_pane_that_carries_an_agents_identity() -> None:
    console = RecordingConsole((_slot("%3", _A), _feed("%2"), _home(_A, "%1", None)))

    await composer(console).ensure()

    assert console.marked == []


async def test_ensure_marks_nothing_when_a_surface_pane_already_exists_anywhere() -> None:
    """Including one parked out of the console by an exchange — which is the whole point.

    A console showing an agent has its surface in that agent's window. If `ensure` looked
    only at the console, it would find an unmarked-by-its-reckoning slot and mark the agent.
    """
    console = RecordingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True))
    )

    await composer(console).ensure()

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

    async def console_windows(self) -> tuple[tuple[int, SessionId | None], ...]:
        return ((0, None),)

    async def link_session_window(self, session_id: SessionId) -> None:
        return None

    async def unlink_console_window(self, index: int) -> None:
        return None


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


async def test_the_other_writer_killing_the_pane_outright_still_brings_the_surface_back() -> None:
    """The variant where there is no dead pane to detect, only an absence.

    A force stop removes the pane rather than preserving it, so the console's slot is taken
    by whichever pane tmux shifts into position 0 — a console pane of its own, carrying no
    identity. Detected by identity alone this reads as a resting console, while the projects
    surface sits in a window whose session is gone. What actually says "not at rest" is the
    surface being somewhere other than the slot.
    """
    console = _SyncingConsole(
        (_slot("%2"), _home(_A, "%1", None, surface=True))
    )
    console.arrangement = (
        HostedPane(None, True, 0, 0, "%2", None, False),
        _home(_A, "%1", None, surface=True),
    )

    await composer(console).sync(())

    assert console.swaps == [("%1", "%2")]


async def test_a_sync_on_a_resting_console_moves_nothing_for_the_other_writer() -> None:
    console = _SyncingConsole((_slot("%1", surface=True), _feed("%2"), _home(_A, "%3", _A)))

    await composer(console).sync((_record(_A, SessionState.RUNNING),))

    assert console.swaps == []
