"""Sealed typed command DTOs; no raw terminal strings exist in this surface."""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.domain.conversations import ResolvedConversation
from remote_agents.domain.external_sessions import (
    ExternalSessionState,
    ExternalStopEligibility,
    ResolvedExternalSession,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.remote_control import RemoteControlState


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
class AdoptionCommand:
    """A server-resolved external candidate that must be rechecked before resuming."""

    external: ResolvedExternalSession
    idempotency_key: str

    def __post_init__(self) -> None:
        if (
            self.external.summary.state is not ExternalSessionState.RUNNING_EXTERNALLY
            or self.external.summary.project_id is None
            or self.external.provider_conversation_id is None
        ):
            raise ValueError("external session is not eligible for safe handoff")


@dataclass(frozen=True, slots=True)
class ExternalStopCommand:
    """Server-resolved input for a future fixed terminate-then-resume transaction."""

    external: ResolvedExternalSession
    conversation: ResolvedConversation
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.external.summary.stop_eligibility is ExternalStopEligibility.READ_ONLY:
            raise ValueError("external session is read-only evidence")
        if self.external.identity is None:
            raise ValueError("external session lacks revalidatable process identity")
        if self.conversation.summary.profile_id != self.external.summary.profile_id:
            raise ValueError("selected resume profile must match the external session")


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


@dataclass(frozen=True, slots=True)
class RemoteControlCommand:
    session_id: SessionId
    desired_state: RemoteControlState
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.desired_state is RemoteControlState.UNKNOWN:
            raise ValueError("remote control state must be active or inactive")
