"""Typed, minimally disclosed records for resumable provider conversations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from string import ascii_lowercase, digits

from remote_agents.domain.models import ProfileId, ProjectId

_MAX_DISPLAY_DESCRIPTION = 120


class ConversationState(StrEnum):
    """Truthful states for provider conversations discovered locally."""

    RESUMABLE = "resumable"


@dataclass(frozen=True, slots=True)
class ConversationReference:
    """Server-resolved opaque key safe to place in callback state."""

    value: str

    def __post_init__(self) -> None:
        if not self.value.startswith("c-") or not 18 <= len(self.value) <= 66:
            raise ValueError("conversation reference must be a bounded opaque token")
        allowed = ascii_lowercase + digits
        if any(character not in allowed for character in self.value[2:]):
            raise ValueError("conversation reference must be a bounded opaque token")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ProviderConversationId:
    """Adapter-discovered provider identity; it is never Telegram input."""

    value: str

    def __post_init__(self) -> None:
        if (
            not self.value
            or len(self.value) > 256
            or any(not character.isprintable() or character.isspace() for character in self.value)
        ):
            raise ValueError("provider conversation ID must be a bounded visible token")


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """Selection metadata: an opaque reference out, the provider ID never in.

    The old summary line said "safe selection metadata", which read as a claim about the
    whole record and was only ever true of half of it. `reference` is the server-resolved
    opaque key, `ProviderConversationId` is not a field here at all, and that boundary is
    enforced and covered by `tests/security/test_session_catalog.py`. `description` is a
    weaker claim and is now written as one: it is the owner's own last prompt or generated
    title (`adapters/agents/claude/sessions.py` `_resume_description`), checked below only
    for length and printability and never for content, so it can carry a filesystem path
    the owner typed. Neither surface filters it: the terminal renders the whole string
    (`adapters/tui/model.py` `conversation_row`) and Telegram a 48-character prefix
    (`adapters/telegram/service.py` `_resume_button_text`) -- shorter, but not screened.
    Filtering was considered and declined -- "path-shaped" has no clean definition, and a
    picker row redacted into ambiguity is worse at the one job the list has (BL-007).
    """

    reference: ConversationReference
    profile_id: ProfileId
    project_id: ProjectId | None
    state: ConversationState
    updated_at: datetime
    description: str | None = None

    def __post_init__(self) -> None:
        if self.description is None:
            return
        if (
            not self.description
            or len(self.description) > _MAX_DISPLAY_DESCRIPTION
            or any(not character.isprintable() for character in self.description)
        ):
            raise ValueError("conversation description must be bounded printable text")


def display_description(value: object) -> str | None:
    """Normalize a provider title to a bounded single line.

    "Owner-approved" was the old wording and it overstated the case: nothing approves this
    text. It is whatever the owner last typed at the agent, read back out of the provider's
    own transcript. Bounding and whitespace collapsing are the whole of what happens to it.
    """
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    return normalized[:_MAX_DISPLAY_DESCRIPTION]


@dataclass(frozen=True, slots=True)
class ResolvedConversation:
    """Internal mapping from an opaque selection to a provider-owned source ID."""

    summary: ConversationSummary
    provider_conversation_id: ProviderConversationId


@dataclass(frozen=True, slots=True)
class ConversationCataloguePage:
    """One bounded, one-indexed page of safe conversation metadata."""

    conversations: tuple[ConversationSummary, ...]
    page: int
    page_count: int
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if self.page < 1 or self.page_count < 1 or self.page > self.page_count:
            raise ValueError("catalogue page bounds are invalid")


@dataclass(frozen=True, slots=True)
class ProfileResumeCapability:
    """Truthful, per-profile availability instead of a version allowlist."""

    profile_id: ProfileId
    catalogue_available: bool
    selected_resume_available: bool
    reason: str | None = None
