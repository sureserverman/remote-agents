"""Presentation-side console operations the application composes tabs over.

A deliberately separate port from `terminal.py`: everything here is about *showing*
sessions — a console session, its panes and windows, a client's focus — and none of it may
ever become something a session's lifecycle depends on (DEC-006). The tmux adapter's
gateway satisfies this protocol structurally.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from remote_agents.domain.models import SessionId


class ConsolePaneSlot(Enum):
    """Which of the console's three panes a pane is — by what it *is*, not where it sits.

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
    FEED = "feed"


class ConsoleBindingAction(Enum):
    """What one console root binding does — a closed set, not a description.

    A binding's action decides tmux argv, so it is chosen from here rather than passed as
    free text (DEC-001). Two members, because the console's whole key budget is two keys.
    """

    SHOW_PROJECTS = "show_projects"
    """Return the projects surface to the console's left slot, wherever an exchange left it.

    It runs *our own program* rather than a tmux command, and that is forced rather than
    chosen: tmux can select a window by itself, but it cannot read our pane marks and work
    out which exchange brings the surface home. Under the tab model this key was
    `select-window 0`, which under the swap model selects the window the owner is already on.
    """

    FOCUS_NEXT_PANE = "focus_next_pane"
    """Move focus to the next pane of the current window, cycling.

    One key for three panes: cycling reaches any of them in at most two presses, where a key
    per pane would spend three of the agent's keys on a second way to do the same thing.

    Pressed outside the console it acts on whatever window that client is on. A managed
    session's window is usually one pane, where cycling changes nothing — but this project
    treats an operator's hand-split pane as ordinary, and the surface hands out an attach
    command for precisely that kind of direct connection, so "a no-op outside the console" is
    not promised. What is promised is that it only ever moves focus.
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
    """Which of the console's three panes this is, by its own mark, or None for anything else.

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
        self, key: str, action: ConsoleBindingAction, command: tuple[str, ...] = ()
    ) -> None: ...

    async def console_windows(self) -> tuple[tuple[int, SessionId | None], ...]: ...

    async def link_session_window(self, session_id: SessionId) -> None: ...

    async def unlink_console_window(self, window_index: int) -> None: ...

    async def select_console_window(self, window_index: int) -> None: ...

    async def switch_client_to_session(self, session_id: SessionId) -> None: ...

    async def console_active_window(self) -> int | None: ...

    async def display_message(self, text: str) -> None: ...

    async def pane_arrangement(self) -> tuple[HostedPane, ...]: ...

    async def swap_panes(self, source_pane: str, target_pane: str) -> None: ...

    async def mark_console_slot(self, pane_id: str, slot: ConsolePaneSlot) -> None: ...
