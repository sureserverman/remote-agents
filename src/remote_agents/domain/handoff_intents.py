"""Durable intent state around the irreversible external-stop boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from remote_agents.domain.external_sessions import ExternalProcessIdentity
from remote_agents.domain.models import ProfileId, ProjectId


class HandoffState(StrEnum):
    REQUESTED = "requested"
    STOP_SENT = "stop_sent"
    RESUMED = "resumed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class HandoffIntent:
    intent_id: str
    profile_id: ProfileId
    project_id: ProjectId
    conversation_source_id: str
    process: ExternalProcessIdentity
    state: HandoffState

