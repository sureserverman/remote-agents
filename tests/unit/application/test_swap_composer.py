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

import pytest

from remote_agents.application.console import ConsoleComposer
from remote_agents.domain.models import SessionId
from remote_agents.ports.console import HostedPane
from remote_agents.ports.terminal import TerminalTargetMissing

_A = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_B = SessionId.parse("11234567-89ab-cdef-0123-456789abcdef")
_LEGACY = SessionId.parse("21234567-89ab-cdef-0123-456789abcdef")


def _slot(pane_id: str, identity: SessionId | None = None) -> HostedPane:
    """The console's left slot: window 0, pane index 0."""
    return HostedPane(None, True, 0, 0, pane_id, identity)


def _feed(pane_id: str) -> HostedPane:
    """A second console pane, so the window is never one pane away from being empty."""
    return HostedPane(None, True, 0, 1, pane_id, None)


def _home(session_id: SessionId, pane_id: str, identity: SessionId | None) -> HostedPane:
    """A pane hosted by one managed session's own window."""
    return HostedPane(session_id, False, 0, 0, pane_id, identity)


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
            ),
            target_pane: HostedPane(
                source.host,
                source.on_console,
                source.window_index,
                source.pane_index,
                target.pane_id,
                target.session_id,
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
    console = RecordingConsole((_slot("%3", _A), _feed("%2"), _home(_A, "%1", None)))

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
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None), _home(_B, "%4", _B))
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
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None), _home(_B, "%4", _B))
    )

    await composer(console).show(_B)

    assert console.swaps[1][1] == "%1", "the slot was remembered rather than re-read"
    assert console.reads >= 2, "the arrangement must be read again between the two exchanges"


async def test_showing_the_agent_already_in_the_slot_exchanges_nothing() -> None:
    console = RecordingConsole((_slot("%3", _A), _feed("%2"), _home(_A, "%1", None)))

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


async def test_an_ambiguous_home_window_refuses_to_guess_which_pane_is_the_surface() -> None:
    """Two unidentified panes in the shown agent's window: an operator hand-split one.

    Picking either would be picking by listing order — the same wrong basis Sub-plan 1
    removed from destruction — and the loser here is a pane swapped into the console window
    in place of the surface. Refusing leaves the console showing the agent, which is a state
    the owner can see and act on.
    """
    console = RecordingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None), _home(_A, "%4", None))
    )

    await composer(console).show_projects()

    assert console.swaps == []


async def test_a_broken_console_degrades_to_a_log_line_and_never_raises() -> None:
    console = RecordingConsole(
        (_slot("%1"), _feed("%2"), _home(_A, "%3", _A)),
        error=TerminalTargetMissing("managed target is gone: %3"),
    )

    await composer(console).show(_A)
    await composer(console).show_projects()


async def test_a_second_call_waits_for_the_first_rather_than_interleaving_its_exchanges() -> None:
    """Two exchanges of one change must not be split by another change's.

    Interleaved, `show(B)` and `show_projects()` can issue A-home, then projects' exchange
    against a slot that is about to be taken, then B-in — an ordering that ends with the
    surface in a home window and an agent nobody asked for in the console. The lock is the
    only thing preventing it, since every step is an awaited round trip.
    """
    console = RecordingConsole(
        (_slot("%3", _A), _feed("%2"), _home(_A, "%1", None), _home(_B, "%4", _B))
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
