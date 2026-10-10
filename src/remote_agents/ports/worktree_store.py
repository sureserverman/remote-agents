"""Session worktrees as stored: one row per folder, and the sessions bound to it.

A folder is its own row rather than a field on `SessionRecord`, whose rebuild is positional
(DEC-115's reason). It is keyed by path, not by session, because a resume and a rollover
successor are new sessions working in the same folder: each is *bound* to it, and the folder
is "unreferenced" only once every session bound to it has ended.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from remote_agents.domain.models import ProjectId, SessionId


@dataclass(frozen=True, slots=True)
class Worktree:
    """One session worktree. `copied` maps each copied repo-relative path to its sha256."""

    path: Path
    project_id: ProjectId
    branch: str
    base_branch: str | None
    """None when the main checkout was detached at creation; such a folder is never removed."""
    base_commit: str
    copied: Mapping[str, str]
    created_at: datetime
    removed_at: datetime | None


class WorktreeStore(Protocol):
    """Worktree rows in the domain store."""

    async def record(self, worktree: Worktree, *, bound_to: SessionId | None) -> None:
        """Write the folder's row and, in the same transaction, bind `bound_to` to it."""
        ...

    async def bind(self, session_id: SessionId, path: Path) -> None:
        """Bind a session to a recorded folder. Rebinding to the same folder is a no-op;
        to a different one, `ValueError`."""
        ...

    async def folder_for(self, session_id: SessionId) -> Worktree | None:
        """The folder this session is bound to, removed or not; None for a regular session."""
        ...

    async def live_for_project(self, project_id: ProjectId) -> tuple[Worktree, ...]:
        """The project's folders not yet removed, oldest first."""
        ...

    async def bound_sessions(self, path: Path) -> tuple[SessionId, ...]:
        """Every session ever bound to the folder."""
        ...

    async def removable_candidates(self) -> tuple[Worktree, ...]:
        """Folders not removed whose bound sessions have all ended. Only a candidate: the
        cleanup pass still asks git before removing anything."""
        ...

    async def mark_removed(self, path: Path, *, at: datetime) -> None:
        """Record that the folder is gone."""
        ...
