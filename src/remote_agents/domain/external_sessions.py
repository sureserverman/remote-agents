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


class ExternalStopEligibility(StrEnum):
    """Closed choices for whether an external row may enter a handoff flow."""

    READ_ONLY = "read_only"
    VERIFIED_SOURCE = "verified_source"
    SELECTION_REQUIRED = "selection_required"


class ExternalStopOutcome(StrEnum):
    """Sanitized terminal outcomes from the one allowed external mutation."""

    EXITED = "exited"
    IDENTITY_CHANGED = "identity_changed"
    PERMISSION_DENIED = "permission_denied"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ExternalProcessIdentity:
    """Adapter-private process identity; it must never be rendered or callback encoded."""

    pid: int
    start_ticks: int
    effective_uid: int
    process_name: str

    def __post_init__(self) -> None:
        if self.pid <= 1:
            raise ValueError("external process identity requires a non-service PID")
        if self.start_ticks < 0 or self.effective_uid < 0:
            raise ValueError("external process identity has invalid immutable metadata")
        if not self.process_name or len(self.process_name) > 64:
            raise ValueError("external process identity requires a bounded process name")


@dataclass(frozen=True, slots=True)
class ExternalStopResult:
    """The controller reports a bounded outcome, never a raw signal or process detail."""

    outcome: ExternalStopOutcome


@dataclass(frozen=True, slots=True)
class ExternalProcessControlCapability:
    """Feature-probed diagnostic state for the fixed Linux control adapter."""

    pidfd_available: bool
    psutil_available: bool

    @property
    def backend(self) -> str | None:
        if self.pidfd_available:
            return "pidfd"
        if self.psutil_available:
            return "psutil"
        return None


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
    stop_eligibility: ExternalStopEligibility = ExternalStopEligibility.READ_ONLY


@dataclass(frozen=True, slots=True)
class ResolvedExternalSession:
    """Adapter-private mapping needed to recheck a safe-handoff candidate later."""

    summary: ExternalSessionSummary
    pid: int
    provider_conversation_id: ProviderConversationId | None
    identity: ExternalProcessIdentity | None = None
