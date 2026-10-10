"""Session worktrees as stored: one row per folder, and the sessions bound to it.

A folder is its own row rather than a field on `SessionRecord`, whose rebuild is positional
(DEC-115's reason). It is keyed by path, not by session, because a resume and a rollover
successor are new sessions working in the same folder: each is *bound* to it, and the folder
is "unreferenced" only once every session bound to it has ended.

**A folder is never without a session.** `record` writes the row and its first binding in one
transaction, so no reader in any process sees a fresh folder nobody is bound to -- which would
read as merged (its tip is its base) and clean, the two things the cleanup pass removes on.

**A removal is claimed, and a claim and a bind exclude each other.** The candidate list is a
snapshot, and the surface's process can resume a session into a folder while `serve` is
between its git checks and `worktree remove`. `claim_for_removal` and `bind` are each one
conditional statement over the same row, so whichever lands first wins and the other is
refused.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from remote_agents.domain.models import ProjectId, SessionId

ProjectWorktreeFolders = Callable[[ProjectId], Awaitable[tuple[Path, ...]]]
"""A project's live (not removed) session worktree folders, oldest first. What a resume list
reads beside the project's own folder, so a worktree session's conversation can be found."""


class WorktreeUnavailable(Exception):
    """The folder is not recorded, already removed, or claimed for removal; nothing was bound."""


@dataclass(frozen=True, slots=True)
class Worktree:
    """One session worktree. `copied` maps each copied repo-relative path to its sha256."""

    path: Path
    project_id: ProjectId
    branch: str
    base_branch: str | None
    """None when the main checkout was detached at creation; such a folder is never a removal
    candidate."""
    base_commit: str
    copied: Mapping[str, str]
    created_at: datetime
    removed_at: datetime | None


class WorktreeStore(Protocol):
    """Worktree rows in the domain store."""

    async def record(self, worktree: Worktree, *, bound_to: SessionId) -> None:
        """Write the folder's row and bind `bound_to` to it, in one transaction."""
        ...

    async def bind(self, session_id: SessionId, path: Path) -> None:
        """Bind a session to a recorded, live, unclaimed folder.

        Rebinding to the same folder is a no-op. A different folder raises `ValueError`; a
        folder not recorded, removed or claimed raises `WorktreeUnavailable`."""
        ...

    async def folder_for(self, session_id: SessionId) -> Worktree | None:
        """The folder this session is bound to, removed or not; None for a regular session."""
        ...

    async def live_for_project(self, project_id: ProjectId) -> tuple[Worktree, ...]:
        """The project's folders not yet removed, oldest first."""
        ...

    async def bound_folders(self) -> Mapping[SessionId, Worktree]:
        """Every binding, with its folder removed or not, in one read: what a pass over every
        session asks instead of one `folder_for` per row (BL-126)."""
        ...

    async def bound_sessions(self, path: Path) -> tuple[SessionId, ...]:
        """Every session ever bound to the folder."""
        ...

    async def removable_candidates(self) -> tuple[Worktree, ...]:
        """Folders not removed, not claimed, with a base branch, and whose bound sessions --
        at least one -- have all ended. Only a candidate: the cleanup pass claims it, then
        asks git, before removing anything."""
        ...

    async def claim_for_removal(self, path: Path, *, at: datetime) -> bool:
        """Claim a folder that is still a candidate. False when it no longer is: a session was
        bound or resumed into it since the candidates were read, or another pass holds it."""
        ...

    async def release_claim(self, path: Path) -> None:
        """Give a claim back: a git guard said no, so the folder stays and stays bindable."""
        ...

    async def release_stale_claims(self) -> int:
        """Give back every claim on a folder not removed: claims outlive only a pass that died
        between claiming and finishing. Returns how many were released."""
        ...

    async def mark_removed(self, path: Path, *, at: datetime) -> None:
        """Record that the folder is gone."""
        ...
