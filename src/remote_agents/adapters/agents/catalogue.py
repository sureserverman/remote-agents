"""Route a profile-qualified selection to its one reviewed provider catalogue."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ProfileResumeCapability,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.conversation_catalog import ConversationCatalog
from remote_agents.ports.worktree_store import ProjectWorktreeFolders

_LOG = logging.getLogger(__name__)


class ProfileConversationCatalogue:
    """Keep provider identities isolated while exposing one typed catalogue port."""

    def __init__(self, catalogues: Mapping[ProfileId, ConversationCatalog]) -> None:
        self._catalogues = dict(catalogues)

    async def list_conversations(
        self,
        *,
        profile_id: ProfileId | None,
        project_id: ProjectId | None,
        page: int,
        page_size: int,
    ) -> ConversationCataloguePage:
        if profile_id is None:
            return ConversationCataloguePage((), page, 1, "profile_required")
        catalogue = self._catalogues.get(profile_id)
        if catalogue is None:
            return ConversationCataloguePage((), page, 1, "profile_not_supported")
        return await catalogue.list_conversations(
            profile_id=profile_id, project_id=project_id, page=page, page_size=page_size
        )

    async def resolve_conversation(
        self, reference: ConversationReference
    ) -> ResolvedConversation | None:
        for catalogue in self._catalogues.values():
            resolved = await catalogue.resolve_conversation(reference)
            if resolved is not None:
                return resolved
        return None

    async def resume_capabilities(self) -> tuple[ProfileResumeCapability, ...]:
        capabilities = []
        for catalogue in self._catalogues.values():
            capabilities.extend(await catalogue.resume_capabilities())
        return tuple(capabilities)


async def project_folders(
    projects: tuple[tuple[ProjectId, Path], ...],
    worktree_folders: ProjectWorktreeFolders | None,
) -> tuple[tuple[ProjectId, Path, Path | None], ...]:
    """Every folder a project's conversations can be filed under: its own, then each live
    session worktree (BL-116). The third item is the worktree, None for the project's own
    folder, and is what a conversation found there carries into its resume."""
    folders: list[tuple[ProjectId, Path, Path | None]] = []
    for project_id, path in projects:
        folders.append((project_id, path, None))
        if worktree_folders is None:
            continue
        try:
            worktrees = await worktree_folders(project_id)
        except Exception:
            # The worktree rows are the domain store, which another process writes: a locked or
            # unreadable read costs this listing its worktree conversations, never the
            # project's own or another provider's.
            _LOG.exception("worktree folders of %s could not be read; listed without", project_id)
            continue
        # Resolved, as the pane's cwd is: that is the folder the agent records its
        # conversations under, and a provider that filters by exact cwd misses any other
        # spelling of it.
        folders.extend(
            (project_id, worktree.resolve(strict=False), worktree.resolve(strict=False))
            for worktree in worktrees
        )
    return tuple(folders)
