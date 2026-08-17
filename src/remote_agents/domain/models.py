"""Immutable, technology-neutral values used by the session lifecycle."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from string import ascii_lowercase, digits
from uuid import UUID, uuid4

from remote_agents.domain.remote_control import RemoteControlState

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


class OrphanProvenance(StrEnum):
    """Which producer put a record into ORPHANED, because the two get different actions.

    ORPHANED conflates two situations that reconciliation can already tell apart and then
    forgets (DEC-020). `ADOPTED` is a trusted managed pane found with **no record at all**
    and taken into the register — frequently a live agent the database lost, and the case
    that justifies offering a force stop. `AMBIGUOUS` is a record whose pane was found but
    was neither live nor preserved, where the evidence supports no action at all.

    Only the *policy* meaning belongs here; which rows may be acted on is
    `session_actions.py`'s to say.
    """

    ADOPTED = "adopted"
    AMBIGUOUS = "ambiguous"


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
    orphan_provenance: OrphanProvenance | None = None
    """Which producer put this record into ORPHANED, or `None` if none did.

    `None` has three causes, and they are worth keeping straight because all three take the
    same conservative branch and only one of them is ordinary:

    1. **This record has never been ORPHANED** — the ordinary case. Both producers stamp:
       `reconcile._save_trusted_orphan` writes `ADOPTED` when it creates a record, and
       `record_event` writes `AMBIGUOUS` when a transition lands an existing record there.
    2. **The row predates migration 6.** Deliberate, and the subject of the next paragraph.
    3. **The stored value was not one this build recognizes** — a hand-edited row, or one
       written by a newer build and read back after a downgrade. `_provenance_from_row` logs
       it and falls to `None` rather than raising, because raising would cost the caller
       every *other* session on the page.

    Cause 3 is the one an earlier version of this docstring denied, claiming `None` meant
    exactly one thing. The conservative fallback that creates it was added to this same file's
    sibling in the same commit, and `tests/integration/sqlite/test_orphan_provenance.py`
    demonstrates it against a genuinely ORPHANED post-migration record.

    A row that predates migration 6 also reads `None`, and that is the one genuine ambiguity.
    It is deliberate: provenance cannot be back-derived, because once a pane is adopted a
    record exists and reconciliation matches it by id from then on, never seeing an unknown
    pane again (DEC-020). Such a row takes the conservative branch rather than gaining a
    destructive action on the strength of a guess.

    It outlives ORPHANED. DEC-020 gives the state one way out, so a force-stopped adopted
    record reaches ENDED still carrying `ADOPTED` — which is what lets the audit trail answer
    *what was killed*, rather than only that something was.
    """
    remote_control_state: RemoteControlState | None = None
    """The last Remote Control state this service *observed* for the pane, or `None`.

    Appended after `orphan_provenance` deliberately, and the reason is written one file over
    in `tests/integration/sqlite/test_orphan_provenance.py`: `record_event` and `set_label`
    rebuild this record positionally, so an appended field is exactly the shape those two
    silently drop. That test exists because it happened; this field is covered by the same
    shape of test rather than trusting that it will not happen again.

    `None` means nobody knows, and it has three causes that all take the same branch: the
    session has never been toggled, the row predates migration 7, or a toggle came back
    UNKNOWN. Every surface answers unknown by offering **both** Remote Control actions, which
    is what all of them did before anything was stored — so the fallback is the old
    behaviour rather than a new failure mode.

    Deliberately not authoritative. It records what a toggle *returned*; a pane can be
    changed from inside the session without this service seeing it. So it is grounds for
    hiding the action that would do nothing, never a claim about the pane right now.
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
