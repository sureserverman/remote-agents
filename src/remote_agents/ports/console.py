"""Presentation-side console operations the application arranges panes with.

A deliberately separate port from `terminal.py`: everything here is about *showing* sessions
— the console session, its panes, the keys it binds — and none of it may ever become
something a session's lifecycle depends on (DEC-006). The tmux adapter's gateway satisfies
this protocol structurally.

It described "composing tabs" until Sub-plan 3's Task 2.4 retired that mechanism. The
vocabulary is panes now: split them, mark them with what they are, exchange one for another.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from remote_agents.domain.models import SessionId


class ConsolePaneSlot(Enum):
    """Which of the console's panes a pane is — by what it *is*, not where it sits.

    **The count lives in this enum and is not written out anywhere.** It said "three" here
    while carrying four members, having gained `LIMITS` on 2026-08-31, and a live test that had
    spelled `3` out was red on `main` for four days behind the opt-in flag. Prose restating the
    size of the thing it is describing is a second declaration that nothing keeps true.

    Position answers "which pane is the left slot", which is the question an exchange asks.
    It cannot answer "which pane is missing", because a console down to two panes has two
    positions and three candidates. So each pane carries its slot as a pane-scoped mark, the
    same mechanism and the same reason as the projects surface's own (DEC-040): a pane is
    found by what it is, and an exchange carries the mark with it.

    `PROJECTS` keeps the value `surface`, which is what the mark already held when it named
    only one pane. That is a wire value, so a console already running on this host keeps
    decoding after an upgrade. Renaming it would strand such a console rather than damage it:
    its projects pane carries a mark, so nothing would adopt it, and nothing would rebuild
    beside it either — the slot would simply read as missing forever. A free compatibility win
    for the cost of one member whose name and value differ.
    """

    PROJECTS = "surface"
    SESSIONS = "sessions"
    LIMITS = "limits"
    FEED = "feed"


class ConsoleKeyTable(Enum):
    """Which tmux key table a console binding goes in — and it decides what the key costs.

    An enum rather than the two strings, beside `ConsolePaneSlot` and `ConsoleBindingAction`
    for the same reason they are enums: the value reaches tmux argv, so it is chosen from a
    closed set rather than passed as text (DEC-001's typed ports). The failure it forecloses is
    quiet — `"Prefix"` type-checks, passes every test that does not construct it, raises at
    install time, and is then swallowed by `ConsoleComposer`'s "the console stands without it",
    leaving a console that looks fine with a chord that does nothing but a log line.
    """

    ROOT = "root"
    """`bind-key -n`: no prefix. A key every agent on this server can never receive, for as
    long as it is bound — the budget DEC-041 fixes at one."""

    PREFIX = "prefix"
    """`bind-key -T prefix`: costs an agent nothing, because tmux intercepts the prefix in the
    *client* before any key reaches a pane. Paid for in the owner's memory instead."""


class ConsoleBindingAction(Enum):
    """What one console binding does — a closed set, not a description.

    Root *and* prefix since the Alt layer: `SHOW_PROJECTS` is the root key DEC-041's budget is
    spent on, and `FORWARD_TO_SESSIONS` is prefix-only and refused anywhere else. Which table a
    binding goes in is `ConsoleKeyTable`, not this.

    A binding's action decides tmux argv, so it is chosen from here rather than passed as
    free text (DEC-001).

    **One member, and it used to be two.** A `FOCUS_NEXT_PANE` action bound a second root key
    to `select-pane -t :.+`, on the premise that a displayed agent consumes the prefix key
    along with everything else the owner types. That premise is false — tmux intercepts the
    prefix in the *client*, before any key reaches the pane, so `prefix + o` already cycles
    the console's panes and costs no agent anything. The action is removed rather than
    left unbound: an unbindable member invites the next author to spend a key on the argument
    that was just disproved.
    """

    FORWARD_TO_SESSIONS = "forward_to_sessions"
    """Resend this key to the console's sessions pane, wherever it currently is.

    The prefix table's action, and the answer to the one place the Alt layer cannot reach: a
    displayed agent owns the left pane's keyboard, so `alt+s` typed there goes to the agent.
    `prefix` + the chord costs the agent nothing — DEC-041's own finding is that tmux
    intercepts the prefix in the *client*, before any key reaches the pane — and lands on the
    pane that already handles the bare row keys.

    Resolved at press time by the pane's slot mark rather than by a pane id captured at install:
    the sessions pane can be rebuilt while the binding stands, and a stale id would forward the
    key into whatever now holds that number. tmux does the lookup itself, filtering
    `list-panes -a` on that mark, so no *pane id* of ours has to be right when the key lands.

    **One name does have to be right, and it is the price of failing closed.** A tmux key table
    belongs to the server, and managed agents attach to that same server — so the binding also
    asks the pressing client which session it is attached to, and does nothing unless that is
    the console (DEC-073(3)). Renaming the console session therefore makes every chord on this
    route inert rather than making it fire from the wrong place.

    The mark's own name is spelled once, in the codec that writes it — see
    `test_the_mark_vocabulary_has_one_home.py`, which caught this docstring spelling it a second
    time, which is exactly the drift it exists for.
    """

    SHOW_PROJECTS = "show_projects"
    """Return the projects surface to the console's left slot, wherever an exchange left it.

    It runs *our own program* rather than a tmux command, and that much is forced: tmux can
    select a window by itself, but it cannot read our pane marks and work out which exchange
    brings the surface home. Under the tab model this key was `select-window 0`, which under
    the swap model selects the window the owner is already on.
    """


@dataclass(frozen=True, slots=True)
class HostedPane:
    """One pane, where it is being shown, and whose it is — the whole arrangement, per pane.

    The composer derives every answer it needs from a tuple of these rather than holding
    any of them: which pane is in the left slot, which agent is displayed there, and where
    the surface that used to be there is parked. That is not tidiness — a second writer can
    stop the displayed session and a crash can leave the panes anywhere, so an answer taken
    once is an answer nothing will correct.
    """

    host: SessionId | None
    """The **managed session** whose window is showing this pane, or None for any other host.

    Not necessarily the session that owns the pane — that is the point of the swap model: an
    agent's pane can be hosted by the console while its own session hosts the surface that was
    displaced. Decoded to an identity by the adapter rather than reported as a terminal's
    session name, because a composer that matched on names would be spelling one adapter's
    conventions in the application layer (DEC-001). Presentation only, never a lifecycle
    input, for the reason `TerminalObservation.host_session` states.
    """

    on_console: bool
    """Whether the console itself is the host. The third answer `host` cannot give: the
    console is not a managed session and never will be — its name is outside the managed
    namespace by construction, which is what keeps lifecycle code from addressing it."""

    window_index: int
    pane_index: int
    """Position within the window, which is what the left slot *is*. A pane id names a pane
    and follows it out of the console on the next exchange; the slot stays where it is."""

    pane_id: str

    session_id: SessionId | None
    """The identity this pane carries **in its own right**, or None.

    None covers the two cases a caller must not tell apart by guessing: a pane that is not a
    managed agent at all (a console surface, an operator's split), and a pane whose session
    is marked under the old session-scoped schema, which names no pane and cannot be
    displayed by exchange. An inherited mark is never reported here — it says which session's
    window a pane sits in, which `host` already answers (DEC-038).
    """

    surface: bool = False
    """Whether this pane is the console's own projects surface, by its own mark.

    The counterpart to `session_id` for the pane on the other end of every exchange. An agent
    is found by the identity it carries; the surface has to be findable the same way, because
    after an exchange it is living in some agent's window and "the pane with no identity" is
    not an answer there — an operator's split makes two of those. Marked, it is exactly one.
    """

    console_slot: str | None = None
    """Which of the console's panes this is, by its own mark, or None for anything else.

    Declared last because the adapter builds this dataclass positionally from one listing
    line, so field order here *is* the wire order there.

    A string rather than a `ConsolePaneSlot` on purpose: the value is decoded from a tmux
    option this process did not necessarily write. A console left running from an older
    version, or one a future version marks differently, has to decode as "not a slot I know"
    rather than raise in the middle of parsing a listing every caller depends on.
    """


@runtime_checkable
class ConsolePort(Protocol):
    """Window-level operations on the one console session."""

    async def console_exists(self) -> bool: ...

    async def create_console(self, dashboard_command: tuple[str, ...], cwd: Path) -> None: ...

    async def split_console_pane(
        self,
        target_pane: str,
        command: tuple[str, ...],
        cwd: Path,
        *,
        vertical: bool,
        percent: int,
        before: bool = False,
    ) -> str: ...

    async def install_console_binding(
        self,
        key: str,
        action: ConsoleBindingAction,
        command: tuple[str, ...] = (),
        table: ConsoleKeyTable = ConsoleKeyTable.ROOT,
    ) -> None: ...

    async def console_zoomed_pane(self) -> str | None: ...

    async def display_message(self, text: str) -> None: ...

    async def pane_arrangement(self) -> tuple[HostedPane, ...]: ...

    async def swap_panes(self, source_pane: str, target_pane: str) -> None: ...

    async def rejoin_console_pane(
        self,
        pane_id: str,
        beside_pane: str,
        *,
        vertical: bool,
        percent: int,
        before: bool = False,
    ) -> None:
        """Move one of the console's own panes back into the console window.

        The operation `swap_panes` cannot express, and the difference is the whole reason this
        exists: a swap *trades*, so it needs something on the far end worth having. When the
        agent that displaced a console pane has its pane **destroyed** -- a cleanup, a force
        stop -- there is nothing left in that window to trade with, and exchanging anyway would
        send a second console pane out to replace the first. The console then shrinks by one
        pane per stop, which is exactly what was observed.

        A move takes one pane and no partner, so it puts the console back together without
        exiling anything. What it leaves behind is a window with no panes, which tmux destroys
        along with the defunct session that held it -- the session being ended already, that is
        the wanted end state rather than a side effect.

        Sized and placed like a split (`vertical`, `percent`, `before`) because it is filling
        the position a split would have built, and the caller is `CONSOLE_LAYOUT` either way.
        """

    async def mark_console_slot(self, pane_id: str, slot: ConsolePaneSlot) -> None: ...

    async def publish_selection(self, session_id: SessionId | None) -> None:
        """Record which session the console's sessions pane has highlighted.

        Console state rather than pane identity, and session-scoped for that reason — the
        codec's `SELECTED_SESSION_OPTION` carries the argument against DEC-038. `None` means
        the cursor rests on nothing and must be published as such: an unpublished clear leaves
        the last selection standing, which is the one thing a chord in another pane must never
        act on (DEC-052, DEC-062).
        """
        ...

    async def read_selection(self) -> SessionId | None:
        """The session the console has selected, or `None` when nothing is.

        Never cached. One read per chord press is the price of never acting on a stale
        selection, and it is a `show-options` against a local socket.
        """
        ...

    async def holds_console_slot(self, pane_id: str) -> bool:
        """Whether this pane is, *right now*, one of the console's own.

        The read side's gate, and it is deliberately two facts rather than one. A pane holds a
        slot mark **and** is shown by the console session: the mark alone is not enough, because
        under DEC-040 the mark travels with the pane — an exchange parks the projects pane in the
        agent's own window and it keeps the mark it was given. Nor is the session alone enough:
        the console window hosts a displaced agent's pane, which is on `ra-console` and is not
        one of ours.

        Asked per press rather than once per process, because an exchange moves a pane while the
        process that owns it keeps running: an answer taken at start-up is wrong for exactly the
        case this exists to refuse.

        Why the read side needs a gate the write side did not: `hosting_mode` classifies by tmux
        socket name, so a plain `remote-agents tui` on the console's server is CONSOLE too, and
        would otherwise answer from the real console's selection from a window that is not one of
        its panes. Two of the keys that resolve through it end a session without asking
        (DEC-018), so who may read the selection is doing the job the confirmation is not.
        """
        ...

    async def normalize_console_layout(
        self, main_percent: int, column: Sequence[tuple[str, int]]
    ) -> None: ...
