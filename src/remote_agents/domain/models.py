"""Immutable, technology-neutral values used by the session lifecycle."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from string import ascii_lowercase, digits
from uuid import UUID, uuid4


class SessionState(StrEnum):
    """Persisted lifecycle states for a managed session."""

    STARTING = "starting"
    RUNNING = "running"
    STOP_REQUESTED = "stop_requested"
    PRESERVED = "preserved"
    FAILED = "failed"
    ENDED = "ended"
    ORPHANED = "orphaned"


@dataclass(frozen=True, slots=True)
class SessionId:
    """Opaque immutable session key, backed by a UUID."""

    value: UUID

    @classmethod
    def new(cls) -> SessionId:
        """Create a new session key."""
        return cls(uuid4())

    @classmethod
    def parse(cls, value: str) -> SessionId:
        """Parse a canonical UUID string into an opaque session key."""
        try:
            parsed = UUID(value)
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("session ID must be a UUID") from error
        if str(parsed) != value:
            raise ValueError("session ID must use canonical UUID form")
        return cls(parsed)

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class ProjectId:
    """Opaque, server-resolved project identifier."""

    value: str

    def __post_init__(self) -> None:
        _validate_token("project ID", self.value)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ProfileId:
    """Stable curated-agent profile identifier."""

    value: str

    def __post_init__(self) -> None:
        _validate_token("profile ID", self.value)

    def __str__(self) -> str:
        return self.value


def _validate_token(name: str, value: str) -> None:
    if not value or len(value) > 64:
        raise ValueError(f"{name} must contain 1 to 64 characters")
    first_characters = ascii_lowercase + digits
    allowed_characters = first_characters + "-"
    if value[0] not in first_characters or not all(
        character in allowed_characters for character in value
    ):
        raise ValueError(f"{name} may contain only letters, digits, and hyphens")


@dataclass(frozen=True, slots=True)
class SessionDisplayIdentity:
    """Human-facing identity that remains unique without a custom label."""

    project_slug: str
    agent_label: str
    mode: str
    sequence: int
    custom_label: str | None = None

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("session sequence must be positive")
        for name, value in (
            ("project slug", self.project_slug),
            ("agent label", self.agent_label),
            ("mode", self.mode),
        ):
            if (
                not value
                or value != value.strip()
                or any(character.isspace() or not character.isprintable() for character in value)
            ):
                raise ValueError(f"{name} must be a non-empty single token")
        if self.custom_label is not None:
            normalized = " ".join(self.custom_label.split())
            if (
                not normalized
                or len(normalized) > 40
                or any(not character.isprintable() for character in normalized)
            ):
                raise ValueError("custom label must contain 1 to 40 visible characters")
            object.__setattr__(self, "custom_label", normalized)

    @property
    def rendered(self) -> str:
        """Render the stable generated part before the optional user label."""
        generated = f"{self.project_slug} · {self.agent_label} · {self.mode} · #{self.sequence}"
        return f"{generated} · {self.custom_label}" if self.custom_label else generated


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """Persistable immutable projection of one managed session."""

    session_id: SessionId
    project_id: ProjectId
    profile_id: ProfileId
    display: SessionDisplayIdentity
    state: SessionState
    created_at: datetime


def allocate_next_sequence(
    records: Iterable[SessionRecord], project_id: ProjectId, profile_id: ProfileId
) -> int:
    """Allocate the next monotonic sequence within a project/profile pair."""
    highest = max(
        (
            record.display.sequence
            for record in records
            if record.project_id == project_id and record.profile_id == profile_id
        ),
        default=0,
    )
    return highest + 1
