"""Application policy for bounded, opaque provider conversation catalogues."""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ProfileResumeCapability,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.conversation_catalog import ConversationCatalog


@dataclass(frozen=True, slots=True)
class ConversationCatalogueQuery:
    """Typed filter values accepted from the presentation boundary."""

    page: int
    page_size: int
    profile_id: ProfileId | None = None
    project_id: ProjectId | None = None

    def __post_init__(self) -> None:
        if self.page < 1 or not 1 <= self.page_size <= 50:
            raise ValueError("conversation catalogue page bounds are invalid")


class ConversationService:
    """Read-only conversation selection policy; provider IDs stay behind its port."""

    def __init__(self, catalog: ConversationCatalog) -> None:
        self._catalog = catalog

    async def catalogue(self, query: ConversationCatalogueQuery) -> ConversationCataloguePage:
        return await self._catalog.list_conversations(
            profile_id=query.profile_id,
            project_id=query.project_id,
            page=query.page,
            page_size=query.page_size,
        )

    async def resolve_for_resume(
        self, reference: ConversationReference
    ) -> ResolvedConversation | None:
        """Resolve a server-issued selection for a later typed continuation command."""
        return await self._catalog.resolve_conversation(reference)

    async def capabilities(self) -> tuple[ProfileResumeCapability, ...]:
        return await self._catalog.resume_capabilities()
