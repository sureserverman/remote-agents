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
    """Safe selection metadata; provider IDs remain server-side."""

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
    """Normalize an owner-approved provider title to a bounded single line."""
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
