"""Which folder a session works in: the one answer every session-meaning reader asks (BL-116).

A regular session works in its project's folder. A session bound to a worktree works in that
worktree -- for its pane, its usage, its resume list and its rollover root alike -- so the
question is asked here and nowhere else. Reading the project mapping directly answers it for
a regular session and silently answers it wrongly for a worktree one.

**A gone worktree has no folder.** Marked removed, or deleted by hand, the answer is None and
never the project folder: the session was given its own folder to keep its work out of the
main checkout, and falling back would put the agent exactly there (`WORKTREE_REMOVED`).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from remote_agents.domain.models import ProjectId, SessionId
from remote_agents.ports.worktree_store import Worktree

WORKTREE_REMOVED_SENTENCE = "This session's worktree has been removed."
"""How both surfaces word a launch or resume refused with `WORKTREE_REMOVED`."""


def live_folder(worktree: Worktree) -> Path | None:
    """The worktree's folder while it is there to work in: not marked removed, and still a
    folder. None otherwise -- the one rule every session-meaning reader applies, so none of
    them can disagree about whether a worktree is gone."""
    if worktree.removed_at is not None or not worktree.path.is_dir():
        return None
    return worktree.path


class _BoundFolders(Protocol):
    async def folder_for(self, session_id: SessionId) -> Worktree | None: ...

    async def live_for_project(self, project_id: ProjectId) -> tuple[Worktree, ...]: ...


class SessionFolders:
    """The session-folder lookup, over the live project mapping and the worktree rows."""

    def __init__(
        self, project_paths: Mapping[ProjectId, Path], store: _BoundFolders | None
    ) -> None:
        # The catalogue's live view, never a copy: a refresh changes it in place, and a
        # project added since composition must be found.
        self._project_paths = project_paths
        # None in a composition that keeps no worktree rows; every session is then regular.
        self._store = store

    async def folder_for(self, session_id: SessionId, project_id: ProjectId) -> Path | None:
        """The worktree when bound and present, None when bound and gone, else the project's
        folder. An unknown project raises `KeyError`, as the mapping does."""
        worktree = None if self._store is None else await self._store.folder_for(session_id)
        if worktree is None:
            return self._project_paths[project_id]
        return live_folder(worktree)

    async def worktree_folders(self, project_id: ProjectId) -> tuple[Path, ...]:
        """The project's live worktree folders, which its resume lists read beside its own
        (`ProjectWorktreeFolders`). None of them with no store."""
        if self._store is None:
            return ()
        live = (live_folder(w) for w in await self._store.live_for_project(project_id))
        return tuple(folder for folder in live if folder is not None)
