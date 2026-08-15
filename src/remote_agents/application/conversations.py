"""Application policy for bounded, opaque provider conversation catalogues."""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
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


def resume_available(summary: ConversationSummary) -> bool:
    """Whether a surface should offer to resume the conversation `summary` describes.

    The single authority over which conversation states may be resumed, here rather than in a
    driver adapter for the same reason `available_actions` is (DEC-001): it is a property of
    the conversation, not of whichever surface happens to render it. This sits beside
    `ConversationService` exactly as `remote_control_available` and `trust_available` sit
    beside `available_actions`, and for the same reason — it is a policy question about one
    record, not an operation on the catalogue.

    **The defect this closes is that the rule was written down twice on one surface and not
    at all on the other.** The bot filtered its list on `state is RESUMABLE` and
    re-checked the same expression at its confirmation; the local surface checked neither, so
    it would have rendered a non-resumable conversation as a choosable row and carried it all
    the way to a launch. Nothing had gone wrong yet only because `ConversationState` has
    exactly one member today — the divergence was real and unobservable, which is the worst
    combination and the reason this exists before a second state does.

    Deliberately a function of the **summary** rather than of the state alone. A resolved
    conversation and a catalogue row both carry one, so both surfaces ask the same question of
    the same shape, and a future rule that needs a second field of the summary can be written
    here without moving every call site.
    """
    return summary.state is ConversationState.RESUMABLE
