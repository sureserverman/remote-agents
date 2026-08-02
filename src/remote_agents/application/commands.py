"""Sealed typed command DTOs; no raw terminal strings exist in this surface."""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.domain.conversations import ResolvedConversation
from remote_agents.domain.models import ProfileId, ProjectId, SessionId


@dataclass(frozen=True, slots=True)
class LaunchCommand:
    project_id: ProjectId
    profile_id: ProfileId
    idempotency_key: str
    label: str | None = None


@dataclass(frozen=True, slots=True)
class ResumeCommand:
    project_id: ProjectId
    profile_id: ProfileId
    conversation: ResolvedConversation
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.conversation.summary.profile_id != self.profile_id:
            raise ValueError("resume profile must match the resolved conversation")


@dataclass(frozen=True, slots=True)
class InspectQuery:
    session_id: SessionId


@dataclass(frozen=True, slots=True)
class GracefulStopCommand:
    session_id: SessionId
    profile_id: ProfileId


@dataclass(frozen=True, slots=True)
class CleanupCommand:
    session_id: SessionId


@dataclass(frozen=True, slots=True)
class ForceStopCommand:
    session_id: SessionId
