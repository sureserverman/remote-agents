"""Typed, content-free records for resumable provider conversations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from string import ascii_lowercase, digits

from remote_agents.domain.models import ProfileId, ProjectId


class ConversationState(StrEnum):
    """Truthful states for provider conversations discovered locally."""

    RESUMABLE = "resumable"
    RUNNING_EXTERNALLY = "running_externally"
    NOT_SAFELY_ADOPTABLE = "not_safely_adoptable"


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
    """Metadata safe to present; it intentionally excludes provider content and IDs."""

    reference: ConversationReference
    profile_id: ProfileId
    project_id: ProjectId | None
    state: ConversationState
    updated_at: datetime


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
