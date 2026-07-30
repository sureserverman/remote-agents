"""Pure lifecycle and identity rules."""

from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
    allocate_next_sequence,
)
from remote_agents.domain.state_machine import (
    InvalidTransition,
    LifecycleEvent,
    Transition,
    transition,
)

__all__ = [
    "ProfileId",
    "ProjectId",
    "SessionDisplayIdentity",
    "SessionId",
    "SessionRecord",
    "SessionState",
    "InvalidTransition",
    "LifecycleEvent",
    "Transition",
    "allocate_next_sequence",
    "transition",
]
