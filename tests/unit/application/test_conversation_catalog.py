from datetime import UTC, datetime

import pytest

from remote_agents.application.conversations import ConversationCatalogueQuery, ConversationService
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProfileResumeCapability,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, ProjectId


class FakeConversationCatalog:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.reference = ConversationReference("c-0123456789abcdef")
        self.summary = ConversationSummary(
            self.reference,
            ProfileId("claude"),
            ProjectId("remote-agents"),
            ConversationState.RESUMABLE,
            datetime.now(UTC),
        )

    async def list_conversations(self, **kwargs: object) -> ConversationCataloguePage:
        self.calls.append(kwargs)
        return ConversationCataloguePage((self.summary,), 1, 1)

    async def resolve_conversation(
        self, reference: ConversationReference
    ) -> ResolvedConversation | None:
        return (
            ResolvedConversation(self.summary, ProviderConversationId("source-123"))
            if reference == self.reference
            else None
        )

    async def resume_capabilities(self) -> tuple[ProfileResumeCapability, ...]:
        return (ProfileResumeCapability(ProfileId("claude"), True, True),)


@pytest.mark.asyncio
async def test_catalogue_forwards_only_typed_filters_to_port() -> None:
    catalog = FakeConversationCatalog()
    service = ConversationService(catalog)

    page = await service.catalogue(
        ConversationCatalogueQuery(1, 20, ProfileId("claude"), ProjectId("remote-agents"))
    )

    assert page.conversations == (catalog.summary,)
    assert catalog.calls == [
        {
            "profile_id": ProfileId("claude"),
            "project_id": ProjectId("remote-agents"),
            "page": 1,
            "page_size": 20,
        }
    ]


@pytest.mark.asyncio
async def test_resolve_requires_a_server_issued_conversation_reference() -> None:
    catalog = FakeConversationCatalog()
    service = ConversationService(catalog)

    resolved = await service.resolve_for_resume(catalog.reference)

    assert resolved is not None
    assert resolved.provider_conversation_id == ProviderConversationId("source-123")


@pytest.mark.asyncio
async def test_capabilities_are_truthful_per_profile() -> None:
    capabilities = await ConversationService(FakeConversationCatalog()).capabilities()

    assert capabilities == (ProfileResumeCapability(ProfileId("claude"), True, True),)


@pytest.mark.parametrize("page,page_size", ((0, 20), (1, 0), (1, 51)))
def test_catalogue_query_bounds_page_size(page: int, page_size: int) -> None:
    with pytest.raises(ValueError, match="page bounds"):
        ConversationCatalogueQuery(page, page_size)
