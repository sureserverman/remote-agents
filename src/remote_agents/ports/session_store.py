"""Technology-neutral durable session-store contract."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionRecord, SessionState
from remote_agents.domain.state_machine import LifecycleEvent


@dataclass(frozen=True, slots=True)
class ProjectUsage:
    """How much one project has been used, counted over every session it ever had.

    Not a `SessionRecord` summary: the sessions it counts are mostly gone. A project earns its
    place in a ranking through launches that have long since ENDED, so this is deliberately a
    read over all rows rather than over the live ones, and it carries no lifecycle state — a
    ranking cannot ask a question about liveness that this answer would tempt it to answer.

    `last_used_at` is timezone-aware, because the only thing a caller does with it is subtract
    it from a clock. A naive value would make that subtraction raise at the point of use,
    which is far from the row that caused it.
    """

    project_id: ProjectId
    session_count: int
    last_used_at: datetime


class SessionStore(Protocol):
    async def next_sequence(self, project_id: ProjectId, profile_id: ProfileId) -> int: ...
    async def save(self, record: SessionRecord) -> None: ...
    async def get(self, session_id: SessionId) -> SessionRecord | None: ...
    async def get_by_resume_source(
        self, profile_id: ProfileId, source_id: str
    ) -> SessionRecord | None: ...
    async def list(
        self, states: Collection[SessionState] | None = None
    ) -> Sequence[SessionRecord]: ...
    async def record_event(self, session_id: SessionId, event: LifecycleEvent) -> SessionRecord: ...
    async def set_label(self, session_id: SessionId, label: str | None) -> SessionRecord: ...
    async def project_usage(self) -> Sequence[ProjectUsage]: ...
    async def claim_idempotency_key(self, key: str) -> bool: ...
