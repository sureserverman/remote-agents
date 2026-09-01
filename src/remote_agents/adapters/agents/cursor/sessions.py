"""Truthful Cursor capability: its interactive picker is not a safe catalogue source."""

from __future__ import annotations

from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ProfileResumeCapability,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, ProjectId


class CursorSessionCatalogue:
    """Never scrape Cursor's interactive `ls` picker for Telegram-selected resume."""

    async def list_conversations(
        self,
        *,
        profile_id: ProfileId | None,
        project_id: ProjectId | None,
        page: int,
        page_size: int,
    ) -> ConversationCataloguePage:
        del profile_id, project_id, page_size
        if page < 1:
            raise ValueError("catalogue page bounds are invalid")
        return ConversationCataloguePage((), page, 1, "structured_catalogue_unavailable")

    async def resolve_conversation(
        self, reference: ConversationReference
    ) -> ResolvedConversation | None:
        del reference
        return None

    async def resume_capabilities(self) -> tuple[ProfileResumeCapability, ...]:
        return (
            ProfileResumeCapability(
                ProfileId("cursor-agent"), False, False, "structured_catalogue_unavailable"
            ),
        )
