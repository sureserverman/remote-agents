"""Route a profile-qualified selection to its one reviewed provider catalogue."""

from __future__ import annotations

from collections.abc import Mapping

from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ProfileResumeCapability,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.conversation_catalog import ConversationCatalog


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
