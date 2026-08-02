"""Content-free observations of local agent processes eligible for a later safe handoff."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from string import ascii_lowercase, digits

from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import ProfileId, ProjectId


class ExternalSessionState(StrEnum):
    """Truthful state before the owner has exited an external process locally."""

    RUNNING_EXTERNALLY = "running_externally"
    NOT_SAFELY_ADOPTABLE = "not_safely_adoptable"


@dataclass(frozen=True, slots=True)
class ExternalSessionReference:
    """Opaque server-issued external-process selection key safe for callbacks."""

    value: str

    def __post_init__(self) -> None:
        if not self.value.startswith("p-") or not 18 <= len(self.value) <= 66:
            raise ValueError("external session reference must be a bounded opaque token")
        allowed = ascii_lowercase + digits
        if any(character not in allowed for character in self.value[2:]):
            raise ValueError("external session reference must be a bounded opaque token")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ExternalSessionSummary:
    """Safe presentation metadata; it excludes PID, terminal, path, and provider identity."""

    reference: ExternalSessionReference
    profile_id: ProfileId
    project_id: ProjectId | None
    state: ExternalSessionState


@dataclass(frozen=True, slots=True)
class ResolvedExternalSession:
    """Adapter-private mapping needed to recheck a safe-handoff candidate later."""

    summary: ExternalSessionSummary
    pid: int
    provider_conversation_id: ProviderConversationId | None
