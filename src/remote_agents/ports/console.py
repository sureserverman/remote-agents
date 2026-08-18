"""Presentation-side console operations the application composes tabs over.

A deliberately separate port from `terminal.py`: everything here is about *showing*
sessions — a console session, linked tab windows, a client's focus — and none of it may
ever become something a session's lifecycle depends on (DEC-006). The tmux adapter's
gateway satisfies this protocol structurally.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from remote_agents.domain.models import SessionId


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
