"""Presentation-side console operations the application composes tabs over.

A deliberately separate port from `terminal.py`: everything here is about *showing*
sessions — a console session, its panes and windows, a client's focus — and none of it may
ever become something a session's lifecycle depends on (DEC-006). The tmux adapter's
gateway satisfies this protocol structurally.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from remote_agents.domain.models import SessionId


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
    window a pane sits in, which `host_session` already answers (DEC-038).
    """


@runtime_checkable
class ConsolePort(Protocol):
    """Window-level operations on the one console session."""

    async def console_exists(self) -> bool: ...

    async def create_console(self, dashboard_command: tuple[str, ...], cwd: Path) -> None: ...

    async def install_console_binding(self, key: str) -> None: ...

    async def console_windows(self) -> tuple[tuple[int, SessionId | None], ...]: ...

    async def link_session_window(self, session_id: SessionId) -> None: ...

    async def unlink_console_window(self, window_index: int) -> None: ...

    async def select_console_window(self, window_index: int) -> None: ...

    async def switch_client_to_session(self, session_id: SessionId) -> None: ...

    async def console_active_window(self) -> int | None: ...

    async def display_message(self, text: str) -> None: ...

    async def pane_arrangement(self) -> tuple[HostedPane, ...]: ...

    async def swap_panes(self, source_pane: str, target_pane: str) -> None: ...
