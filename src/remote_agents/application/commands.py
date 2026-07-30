"""Sealed typed command DTOs; no raw terminal strings exist in this surface."""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.domain.models import ProfileId, ProjectId, SessionId


@dataclass(frozen=True, slots=True)
class LaunchCommand:
    project_id: ProjectId
    profile_id: ProfileId
    idempotency_key: str
    label: str | None = None


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
