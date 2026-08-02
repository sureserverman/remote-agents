"""Read-only provider conversation catalogue boundary."""

from __future__ import annotations

from typing import Protocol

from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ProfileResumeCapability,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, ProjectId


class ConversationCatalog(Protocol):
    """Lists safe summaries and resolves only server-issued opaque references."""

    async def list_conversations(
        self,
        *,
        profile_id: ProfileId | None,
        project_id: ProjectId | None,
        page: int,
        page_size: int,
    ) -> ConversationCataloguePage: ...

    async def resolve_conversation(
        self, reference: ConversationReference
    ) -> ResolvedConversation | None: ...

    async def resume_capabilities(self) -> tuple[ProfileResumeCapability, ...]: ...
