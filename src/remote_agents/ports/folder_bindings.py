"""Sessions bound to a folder outside their project (DEC-131); technology-neutral."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from remote_agents.domain.models import SessionId


class FolderBindings(Protocol):
    """Which folder outside its project a session works in, when it works in one.

    A rollover successor of a session whose handoff writer reported another git top level is
    started there. Only a folder the one acceptance rule passes is ever bound, and a binding is
    written before the session's pane starts (DEC-004).
    """

    async def bind(self, session_id: SessionId, folder: Path) -> bool:
        """Bind the session to `folder`; False when the rule refused it, and nothing was bound.

        A session is bound once, when it is created; a second binding for it raises."""
        ...

    async def folder_for(self, session_id: SessionId) -> Path | None:
        """The session's bound folder, or None when it has none."""
        ...

    async def bound_folders(self) -> dict[SessionId, Path]:
        """Every binding, in one read."""
        ...
