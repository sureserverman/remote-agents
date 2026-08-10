"""Immutable, technology-neutral values used by the session lifecycle."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from string import ascii_lowercase, digits
from uuid import UUID, uuid4

MAX_LABEL_LENGTH = 40
"""The hard bound on a session label. A host may configure something smaller, never larger.

`config.py` clamps `limits.max_label_length` to 1..40, so this is the ceiling rather than a
default that a setting could raise past.
"""


def normalize_label(value: str, *, max_length: int = MAX_LABEL_LENGTH) -> str:
    """Return the stored form of an owner-supplied session label, or raise.

    One rule, for both surfaces and for both moments a label can be set — at launch, and later
    from a session's own menu. It existed twice before: the Telegram adapter carried a private
    `_label`, and this module re-derived the same normalization inline. Two copies of a
    validation rule is one copy plus a future divergence, and the divergence is silent because
    both accept the common case.

    Whitespace is collapsed rather than merely stripped: `"a  b"` and `"a b"` are the same name
    to a reader, and storing the difference makes two sessions that look identical compare as
    though they are not. Empty-after-normalization is rejected because the rendered identity
    joins its parts on `" · "`, and a blank fifth part produces a row that reads back as a
    five-part identity carrying no label.
    """
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("a session label must contain at least one visible character")
    if len(normalized) > max_length:
        raise ValueError(f"a session label must be at most {max_length} characters")
    if any(not character.isprintable() for character in normalized):
        raise ValueError("a session label must contain only printable characters")
    return normalized


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
            object.__setattr__(self, "custom_label", normalize_label(self.custom_label))

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
    resume_profile_id: ProfileId | None = None
    resume_source_id: str | None = None
    terminal_reason: str | None = None
    """The lifecycle event that ended this session, once it has reached a terminal state.

    Without it a session killed out from under the service is indistinguishable from one
    the owner stopped deliberately, because both simply read ENDED.
    """

    def __post_init__(self) -> None:
        if (self.resume_profile_id is None) != (self.resume_source_id is None):
            raise ValueError("resume identity must include both profile and source")
        if self.resume_source_id is not None and (
            not self.resume_source_id
            or len(self.resume_source_id) > 256
            or any(
                not character.isprintable() or character.isspace()
                for character in self.resume_source_id
            )
        ):
            raise ValueError("resume source ID must be a bounded visible token")


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
