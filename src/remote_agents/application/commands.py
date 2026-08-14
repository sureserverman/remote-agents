"""Sealed typed command DTOs; no raw terminal strings exist in this surface."""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.domain.conversations import ResolvedConversation
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
class AnswerTrustCommand:
    """Answer the folder-trust question for one exact session, at most once.

    Carries an idempotency key for the same reason `RemoteControlCommand` does: the button
    that issues it is a durable callback the owner can press twice, and the effect is a
    keypress into somebody's agent. Replaying it would send a second Enter after the dialog
    is gone, which is no longer answering a question -- it is typing into whatever replaced
    it.
    """

    session_id: SessionId
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RemoteControlCommand:
    session_id: SessionId
    desired_state: RemoteControlState
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.desired_state is RemoteControlState.UNKNOWN:
            raise ValueError("remote control state must be active or inactive")
