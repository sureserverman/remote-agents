"""The left pane is exchanged with an agent, and who is in it is always re-read.

`show` and `show_projects` are the whole of the swap model's mechanics. Three rules carry
the risk, and each has a test here that fails without it:

- **Changing agents is two exchanges, never one.** Swapping the console's left pane
  (holding agent A) straight against agent B's pane would put A into B's *home window* —
  A hosted by B's session, two identities crossed, and A unreachable by anything that
  looks for it where it belongs. A goes home first, then B comes in.
- **The left slot is a position, not a pane.** After one exchange the pane that used to sit
  in the slot is living in some agent's home window, so a remembered id names the wrong
  thing. Sub-plan 1's live drive did exactly this and landed the second agent in the first
  agent's window. Every exchange re-reads the arrangement.
- **Who is in the left pane is derived, never believed.** The composer holds no field for
  it: a second process (the bot) can stop the shown session, and a crash can leave the
  arrangement anywhere, so the answer is read from the panes' own marks at every call.

Presentation rules from `ConsoleComposer` still hold and are asserted rather than assumed:
failure degrades to a log line, and the composer writes no record and touches no lifecycle
(DEC-006, DEC-036).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest

from remote_agents.application.commands import (
    CleanupCommand,
    ForceStopCommand,
    GracefulStopCommand,
)
from remote_agents.application.console import ConsoleComposer
from remote_agents.application.services import SessionService
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.state_machine import LifecycleEvent
from remote_agents.ports.console import HostedPane
from remote_agents.ports.terminal import TerminalObservation, TerminalTargetMissing

_A = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_B = SessionId.parse("11234567-89ab-cdef-0123-456789abcdef")
_LEGACY = SessionId.parse("21234567-89ab-cdef-0123-456789abcdef")


def _slot(pane_id: str, identity: SessionId | None = None) -> HostedPane:
    """The console's left slot: window 0, pane index 0.

    Carries the surface mark when it holds no agent, because that is what it then is. The
    surface is found by that mark rather than by being the only unmarked pane in a window —
    an inference that stopped being an answer as soon as anything else shared the window.
    """
    return HostedPane(None, True, 0, 0, pane_id, identity, identity is None)


def _feed(pane_id: str) -> HostedPane:
    """A second console pane, so the window is never one pane away from being empty."""
    return HostedPane(None, True, 0, 1, pane_id, None, False)


def _home(
    session_id: SessionId, pane_id: str, identity: SessionId | None, *, surface: bool = False
) -> HostedPane:
    """A pane hosted by one managed session's own window.

    `surface` is explicit and defaults off. Derived from `identity is None` it silently made
    every unidentified pane the console's surface — including a legacy session's own pane in
    the test below, which is an agent and not the console's anything. A helper that decides a
    mark for you is a helper that will mark the wrong thing eventually.
    """
    return HostedPane(session_id, False, 0, 0, pane_id, identity, surface)


class RecordingConsole:
    """A console that answers a fixed arrangement and applies each exchange to it.

    Applying the swap matters: the whole point of re-reading is that the second read differs
    from the first, so a double that returned a frozen arrangement would let a composer that
    cached the slot pass. This one moves the two panes between their hosts exactly as tmux
    does, which is what makes the crossed-identity test able to fail.
    """

    def __init__(self, arrangement: tuple[HostedPane, ...], *, error: Exception | None = None):
        self.arrangement = arrangement
        self.error = error
        self.swaps: list[tuple[str, str]] = []
        self.reads = 0

    async def pane_arrangement(self) -> tuple[HostedPane, ...]:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.arrangement

    async def swap_panes(self, source_pane: str, target_pane: str) -> None:
        self.swaps.append((source_pane, target_pane))
        if self.error is not None:
            raise self.error
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
    from pathlib import Path

    return ConsoleComposer(console, ("dashboard",), Path("/tmp"))


def _hosts(console: RecordingConsole) -> dict[str, str]:
    return {
        pane.pane_id: "ra-console" if pane.on_console else f"ra-{pane.host}"
        for pane in console.arrangement
    }


async def test_showing_an_agent_exchanges_its_pane_with_the_left_slot() -> None:
    console = RecordingConsole((_slot("%1"), _feed("%2"), _home(_A, "%3", _A)))

    await composer(console).show(_A)

    assert console.swaps == [("%3", "%1")]
    assert _hosts(console) == {"%1": f"ra-{_A}", "%2": "ra-console", "%3": "ra-console"}


async def test_showing_the_projects_surface_issues_the_inverse_exchange() -> None:
    """The surface is found where the exchange left it, not where it started."""
    console = RecordingConsole((_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True)))

    await composer(console).show_projects()

    assert console.swaps == [("%1", "%3")]
    assert _hosts(console) == {"%1": "ra-console", "%2": "ra-console", "%3": f"ra-{_A}"}


async def test_changing_agents_sends_the_first_home_before_bringing_the_second_in() -> None:
    """Two exchanges, in this order, and never the one exchange that crosses them.

    A single `swap_panes("%4", "%3")` would leave agent A's pane hosted by `ra-B` — A is
    then in a window belonging to another session, and B's home window holds a stranger.
    Both sessions still run, which is what makes it silent.
    """
    console = RecordingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True), _home(_B, "%4", _B))
    )

    await composer(console).show(_B)

    assert console.swaps == [("%1", "%3"), ("%4", "%1")], (
        "the shown agent must go home first; a direct exchange crosses two sessions"
    )
    assert _hosts(console) == {
        "%1": f"ra-{_B}",
        "%2": "ra-console",
        "%3": f"ra-{_A}",
        "%4": "ra-console",
    }


async def test_the_second_exchange_targets_the_slot_as_re_read_not_as_remembered() -> None:
    """The concrete failure the position rule prevents, asserted on the argv itself.

    After the first exchange the pane that was in the left slot (`%3`) is living in `ra-A`'s
    window. A composer holding the slot id from before would pass `%3` as the target and put
    agent B into A's home window. The target of the second exchange must be `%1` — the pane
    that is *now* in the slot — which is only knowable by reading again.
    """
    console = RecordingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True), _home(_B, "%4", _B))
    )

    await composer(console).show(_B)

    assert console.swaps[1][1] == "%1", "the slot was remembered rather than re-read"
    assert console.reads >= 2, "the arrangement must be read again between the two exchanges"


async def test_showing_the_agent_already_in_the_slot_exchanges_nothing() -> None:
    console = RecordingConsole((_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True)))

    await composer(console).show(_A)

    assert console.swaps == []


async def test_showing_projects_when_they_are_already_home_exchanges_nothing() -> None:
    console = RecordingConsole((_slot("%1"), _feed("%2"), _home(_A, "%3", _A)))

    await composer(console).show_projects()

    assert console.swaps == []


async def test_a_session_with_no_pane_of_its_own_is_not_shown_and_does_not_raise() -> None:
    """A schema-1 session names no pane, so there is nothing to exchange.

    It stays fully manageable — Sub-plan 1's legacy path reaches it by session target — and
    the console simply cannot display it. Degrading is the DEC-036 answer: a session the
    console cannot show is not a session that stops working.
    """
    console = RecordingConsole((_slot("%1"), _feed("%2"), _home(_LEGACY, "%3", None)))

    await composer(console).show(_LEGACY)

    assert console.swaps == []


async def test_a_hand_split_beside_the_parked_surface_no_longer_stops_the_exchange() -> None:
    """Two unmarked panes in the shown agent's window, and the surface is still exactly one.

    **This test asserted the opposite until Stage 1's gate.** While the surface was identified
    as "the only pane in that window carrying no identity", an operator's hand-split made two
    candidates, and refusing was the right call — picking by listing order is the wrong basis
    Sub-plan 1 removed from destruction, and the loser here is a stranger's pane swapped into
    the console in place of the surface. What the refusal left behind was a console showing an
    agent with **no route back**: `show_projects` and every later `show` returned without
    moving anything until somebody used tmux by hand. The gate evaluator graded that a real
    defect, and the repair was to stop inferring — the console's own surface carries its own
    mark now, so the question the refusal protected against is not asked.
    """
    console = RecordingConsole(
        (
            _slot("%3", _A),
            _feed("%2"),
            _home(_A, "%1", None, surface=True),
            HostedPane(_A, False, 0, 1, "%9", None, False),
        )
    )

    await composer(console).show_projects()

    assert console.swaps == [("%1", "%3")], (
        "the marked surface was not brought back past an unmarked neighbour"
    )


async def test_a_console_with_no_marked_surface_still_refuses_rather_than_guessing() -> None:
    """The case the mark cannot answer, which is the one the refusal is still for.

    A console created before the mark existed, caught while displaced, has no marked pane
    anywhere. Nothing distinguishes the parked surface from anything else in that window, so
    there is still no safe guess — and refusing leaves a console that plainly shows an agent,
    which the owner can see, rather than one that has swapped a stranger's pane into itself.
    """
    console = RecordingConsole(
        (_slot("%3", _A), _feed("%2"), HostedPane(_A, False, 0, 0, "%1", None, False))
    )

    await composer(console).show_projects()

    assert console.swaps == []


async def test_a_broken_console_degrades_to_a_log_line_and_never_raises(caplog) -> None:
    """Both halves of the name, because "never raises" alone passes for a method that does
    nothing at all — and the log line is the only trace a degraded console leaves."""
    console = RecordingConsole(
        (_slot("%1"), _feed("%2"), _home(_A, "%3", _A)),
        error=TerminalTargetMissing("managed target is gone: %3"),
    )

    with caplog.at_level(logging.ERROR, logger="remote_agents.application.console"):
        await composer(console).show(_A)
        await composer(console).show_projects()

    assert caplog.records, "a console failure left no trace at all"


async def test_a_second_call_waits_for_the_first_rather_than_interleaving_its_exchanges() -> None:
    """Two exchanges of one change must not be split by another change's.

    Interleaved, `show(B)` and `show_projects()` can issue A-home, then projects' exchange
    against a slot that is about to be taken, then B-in — an ordering that ends with the
    surface in a home window and an agent nobody asked for in the console. The lock is the
    only thing preventing it, since every step is an awaited round trip.
    """
    console = RecordingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True), _home(_B, "%4", _B))
    )
    gate = asyncio.Event()
    original = console.pane_arrangement
    first = True

    async def blocking_read() -> tuple[HostedPane, ...]:
        nonlocal first
        if first:
            first = False
            await gate.wait()
        return await original()

    console.pane_arrangement = blocking_read  # type: ignore[method-assign]
    one = composer(console)

    changing = asyncio.create_task(one.show(_B))
    await asyncio.sleep(0)
    leaving = asyncio.create_task(one.show_projects())
    await asyncio.sleep(0)
    assert console.swaps == [], "neither call may exchange before the first read returns"

    gate.set()
    await asyncio.gather(changing, leaving)

    assert console.swaps == [("%1", "%3"), ("%4", "%1"), ("%1", "%4")], (
        "the second call's exchange interleaved with the first call's pair"
    )


async def test_the_composer_never_holds_who_is_in_the_left_pane() -> None:
    """The structural half: no attribute may cache the answer the marks already carry.

    Behavioural tests catch a cache that is *wrong*; they cannot catch one that happens to
    be right in every case they exercise. A second process stopping the shown session, or a
    crash mid-exchange, invalidates a remembered answer with nothing calling back — so the
    guarantee is that there is nothing to invalidate.
    """
    console = RecordingConsole((_slot("%1"), _feed("%2"), _home(_A, "%3", _A)))
    one = composer(console)

    await one.show(_A)

    held = {
        name: value
        for name, value in vars(one).items()
        if isinstance(value, SessionId | HostedPane | str) and name != "_jump_home_key"
    }

    assert not any(isinstance(value, SessionId | HostedPane) for value in held.values()), (
        f"the composer is remembering who is shown: {held}"
    )
    # And a bare pane id, which is how the bug would most naturally be written —
    # `self._slot_pane = "%1"`. The first version of this test collected strings and then
    # asserted only against the two rich types, so the one shape most likely to appear
    # slipped through the check named for catching it.
    assert not any(
        isinstance(value, str) and value.startswith("%") for value in held.values()
    ), f"the composer is remembering a pane id: {held}"


@pytest.mark.parametrize("missing", ["slot", "agent"])
async def test_an_arrangement_missing_either_end_exchanges_nothing(missing: str) -> None:
    """A console with no window 0, or an agent that has gone between the read and the call."""
    panes = {
        "slot": (_home(_A, "%3", _A),),
        "agent": (_slot("%1"), _feed("%2")),
    }[missing]
    console = RecordingConsole(panes)

    await composer(console).show(_A)

    assert console.swaps == []


class _StopStore:
    """The narrow slice of the session store the two stop paths touch."""

    def __init__(self, record: SessionRecord) -> None:
        self.record = record
        self.events: list[LifecycleEvent] = []

    async def get(self, session_id: SessionId) -> SessionRecord | None:
        return self.record if session_id == self.record.session_id else None

    async def record_event(self, session_id: SessionId, event: LifecycleEvent) -> SessionRecord:
        self.events.append(event)
        return self.record


class _StopTerminal:
    """A terminal that logs the order of what it was asked to do."""

    def __init__(self, order: list[str], *, preserved: bool = True) -> None:
        self.order = order
        self.preserved = preserved

    async def graceful_stop(self, session_id: SessionId, profile_id: ProfileId):
        self.order.append("graceful_stop")
        return TerminalObservation(session_id, live=False, preserved=self.preserved)

    async def cleanup(self, session_id: SessionId) -> None:
        self.order.append("cleanup")

    async def force_stop(self, session_id: SessionId):
        self.order.append("force_stop")
        return TerminalObservation(session_id, live=False, preserved=False)


def _running(
    session_id: SessionId, state: SessionState = SessionState.RUNNING
) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


def _stop_service(
    order: list[str],
    *,
    hide=None,
    preserved: bool = True,
    state: SessionState = SessionState.RUNNING,
) -> SessionService:
    return SessionService(
        _StopStore(_running(_A, state)),
        _StopTerminal(order, preserved=preserved),
        hide_in_console=hide,
    )


async def test_a_graceful_stop_sends_the_shown_agent_home_before_the_pane_is_destroyed() -> None:
    """The destruction is `cleanup`, and the exchange has to precede it.

    Destroying first kills a pane sitting in the console's own window: under the three-pane
    design that leaves a dead pane where the projects surface belongs, and in a window holding
    only that pane it takes the console session with it, because tmux drops a window's session
    along with its last pane. Either way the owner is left looking at the wreckage of the
    session they just ended.

    Not at the *top* of the stop, deliberately. A graceful stop that times out leaves the
    session running, and hiding it then would pull the agent out of view for a stop that did
    not happen.
    """
    order: list[str] = []

    async def hide(session_id: SessionId) -> None:
        order.append(f"hide:{session_id}")

    await _stop_service(order, hide=hide).graceful_stop(
        GracefulStopCommand(_A, ProfileId("claude"))
    )

    assert order == ["graceful_stop", f"hide:{_A}", "cleanup"], order


async def test_a_graceful_stop_that_times_out_leaves_the_agent_on_screen() -> None:
    """Nothing was destroyed, so nothing should be hidden — the session is still running."""
    order: list[str] = []

    async def hide(session_id: SessionId) -> None:
        order.append(f"hide:{session_id}")

    await _stop_service(order, hide=hide, preserved=False).graceful_stop(
        GracefulStopCommand(_A, ProfileId("claude"))
    )

    assert order == ["graceful_stop"], order


async def test_a_force_stop_sends_the_shown_agent_home_before_the_kill() -> None:
    order: list[str] = []

    async def hide(session_id: SessionId) -> None:
        order.append(f"hide:{session_id}")

    await _stop_service(order, hide=hide).force_stop(ForceStopCommand(_A))

    assert order == [f"hide:{_A}", "force_stop"], order


async def test_a_stop_composed_without_a_console_destroys_exactly_as_it_always_did() -> None:
    """The capability is optional, so a host that wires nothing keeps the old contract."""
    order: list[str] = []

    await _stop_service(order).graceful_stop(GracefulStopCommand(_A, ProfileId("claude")))

    assert order == ["graceful_stop", "cleanup"], order


async def test_a_console_that_cannot_be_moved_never_stops_a_stop() -> None:
    """DEC-006, at the one place it would be most tempting to let presentation win.

    A broken console must cost the owner the tab bar, never a stop. The exchange is attempted
    and its failure is swallowed here rather than in the composer alone, because this method
    is the one that must not raise: a stop that failed because a *display* could not be
    rearranged would be exactly the coupling the decision forbids.
    """
    order: list[str] = []

    async def hide(session_id: SessionId) -> None:
        order.append("hide-failed")
        raise TerminalTargetMissing("managed target is gone: %3")

    await _stop_service(order, hide=hide).force_stop(ForceStopCommand(_A))

    assert order == ["hide-failed", "force_stop"], order


async def test_hiding_a_session_the_console_is_not_showing_issues_no_swap_for_that_stop() -> None:
    """The narrowing that keeps one session's stop from disturbing another's view."""
    console = RecordingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True), _home(_B, "%4", _B))
    )

    await composer(console).hide(_B)

    assert console.swaps == []


async def test_hiding_the_session_that_is_shown_brings_the_surface_back_when_it_stops() -> None:
    console = RecordingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None, surface=True), _home(_B, "%4", _B))
    )

    await composer(console).hide(_A)

    assert console.swaps == [("%1", "%3")]


async def test_a_console_that_never_answers_cannot_hold_up_a_force_stop() -> None:
    """The one that makes this a DEC-006 question rather than a tidiness one.

    `AsyncTmuxRunner.run` awaits `process.communicate()` with no timeout — a hazard DEC-030
    already names in this codebase — so a wedged tmux server makes every console round trip
    block forever. Put an unbounded one between `VERIFIED_FORCE_STOP` and the kill, and a
    force stop on a runaway agent never reaches the agent: the per-session lock stays held,
    and every other composer operation queues behind the same `_links` lock. A stop whose
    forward progress depends on a *display* answering is exactly the coupling DEC-036
    forbids, and this diff is what introduced the await.

    So the wait is bounded where the lifecycle guarantee lives, not only where the
    presentation one does. Asserted by a hook that never resolves.
    """
    order: list[str] = []
    never = asyncio.Event()

    async def hide(session_id: SessionId) -> None:
        order.append("hide-hung")
        await never.wait()

    await asyncio.wait_for(
        _stop_service(order, hide=hide).force_stop(ForceStopCommand(_A)), timeout=10
    )

    assert order == ["hide-hung", "force_stop"], (
        f"the kill did not happen after the console failed to answer: {order}"
    )


async def test_a_console_that_never_answers_cannot_hold_up_a_graceful_stops_cleanup() -> None:
    """The same bound on the other destructive path, where the pane still needs removing."""
    order: list[str] = []
    never = asyncio.Event()

    async def hide(session_id: SessionId) -> None:
        order.append("hide-hung")
        await never.wait()

    await asyncio.wait_for(
        _stop_service(order, hide=hide).graceful_stop(GracefulStopCommand(_A, ProfileId("claude"))),
        timeout=10,
    )

    assert order == ["graceful_stop", "hide-hung", "cleanup"], order


async def test_discarding_a_preserved_pane_also_steps_the_console_aside_before_the_stop() -> None:
    """The third destructive path, missed by this task's first draft.

    `cleanup` removes a pane through the same terminal call `graceful_stop` uses, and it is
    offered from PRESERVED — the state whose pane is still worth showing (DEC-021/DEC-039),
    and which Sub-plan 3 will let the owner display. Every docstring in this task claimed
    "every stop path" while one path destroyed a pane without asking the console to move.
    Found by Tier-1 review; unreachable today only because `show` has no production caller.
    """
    order: list[str] = []

    async def hide(session_id: SessionId) -> None:
        order.append(f"hide:{session_id}")

    await _stop_service(order, hide=hide, state=SessionState.PRESERVED).cleanup(
        CleanupCommand(_A)
    )

    assert order == [f"hide:{_A}", "cleanup"], order
