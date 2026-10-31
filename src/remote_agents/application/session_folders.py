"""Which folder a session works in: the one answer every session-meaning reader asks (BL-116).

A regular session works in its project's folder. A session bound to a worktree works in that
worktree -- for its pane, its usage, its resume list and its rollover root alike -- so the
question is asked here and nowhere else. Reading the project mapping directly answers it for
a regular session and silently answers it wrongly for a worktree one.

**A gone worktree has no folder.** Marked removed, or deleted by hand, the answer is None and
never the project folder: the session was given its own folder to keep its work out of the
main checkout, and falling back would put the agent exactly there (`WORKTREE_REMOVED`).

A session bound to a folder outside its project -- a rollover successor started where its
predecessor's handoff writer reported working (DEC-131) -- works in that folder, and once it is
gone it has no folder either, for the same reason.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from remote_agents.domain.models import ProjectId, SessionId
from remote_agents.ports.worktree_store import Worktree

FOLDER_UNAVAILABLE_SENTENCE = (
    "This conversation's saved folder is unavailable or no longer belongs to this project."
)

WORKTREE_REMOVED_SENTENCE = "This session's worktree has been removed."
"""How both surfaces word a launch or resume refused with `WORKTREE_REMOVED`."""


def live_folder(worktree: Worktree) -> Path | None:
    """The worktree's folder while it is there to work in: not marked removed, and still a
    folder. None otherwise -- the one rule every session-meaning reader applies, so none of
    them can disagree about whether a worktree is gone."""
    if (
        worktree.removed_at is not None
        or not worktree.path.is_dir()
        or worktree.path.resolve() != worktree.path
    ):
        return None
    return worktree.path


class _BoundFolders(Protocol):
    async def folder_for(self, session_id: SessionId) -> Worktree | None: ...

    async def live_for_project(self, project_id: ProjectId) -> tuple[Worktree, ...]: ...


class _OutsideFolders(Protocol):
    async def folder_for(self, session_id: SessionId) -> Path | None: ...

    async def for_project(self, project_id: ProjectId) -> tuple[Path, ...]: ...

    def available(self, folder: Path) -> bool: ...


class SessionFolders:
    """The session-folder lookup, over the live project mapping and the worktree rows."""

    def __init__(
        self,
        project_paths: Mapping[ProjectId, Path],
        store: _BoundFolders | None,
        folders: _OutsideFolders | None = None,
    ) -> None:
        # The catalogue's live view, never a copy: a refresh changes it in place, and a
        # project added since composition must be found.
        self._project_paths = project_paths
        # None in a composition that keeps no worktree rows; every session is then regular.
        self._store = store
        # The folder bindings outside the project (DEC-131); None where none are kept.
        self._folders = folders

    async def folder_for(self, session_id: SessionId, project_id: ProjectId) -> Path | None:
        """In order: the worktree when bound (None when it is gone); the folder outside the
        project when bound (None when it is no longer a folder); else the project's folder. An
        unknown project raises `KeyError`, as the mapping does."""
        worktree = None if self._store is None else await self._store.folder_for(session_id)
        if worktree is not None:
            folder = live_folder(worktree)
            if folder is not None and self._folders is not None:
                return folder if self._folders.available(folder) else None
            return folder
        folder = None if self._folders is None else await self._folders.folder_for(session_id)
        if folder is not None:
            associated = await self._folders.for_project(project_id)
            return folder if folder in associated and self._folders.available(folder) else None
        return self._project_paths[project_id]

    async def worktree_folders(self, project_id: ProjectId) -> tuple[Path, ...]:
        """Live recorded continuation folders beside the project root.

        Includes outside bindings from ended sessions under their recorded project. Keep
        the historical method name for the provider port; all providers use this authority.
        """
        worktrees = () if self._store is None else await self._store.live_for_project(project_id)
        folders = [
            folder
            for w in worktrees
            if (folder := live_folder(w)) is not None
            and (self._folders is None or self._folders.available(folder))
        ]
        if self._folders is not None:
            folders.extend(
                folder
                for folder in await self._folders.for_project(project_id)
                if self._folders.available(folder)
            )
        return tuple(dict.fromkeys(folders))
