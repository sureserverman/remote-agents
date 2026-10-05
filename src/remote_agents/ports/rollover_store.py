"""Durable rollovers: one row per rollover, an append-only history, and lineage read off both.

A rollover is its own object rather than fields on `SessionRecord`, whose rebuild is positional
(`domain/models.py`), so an appended field risks a silent mis-rebuild (DEC-115). Lineage --
"continued from" and "continued as" -- is therefore never stored on a session; it is derived
from COMPLETED rollovers, so a rollover that failed after launching a successor links nothing.

Rows live in the domain store so a change wakes the store watcher on both surfaces (DEC-090).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.rollover import RolloverState


class IllegalRolloverMove(ValueError):
    """An `advance` the domain matrix does not allow; nothing was written."""


@dataclass(frozen=True, slots=True)
class Rollover:
    """One rollover as stored. `plan` is the envelope's path, kept and shown, never opened."""

    id: str
    handoff_id: str | None
    """None only while REQUESTED: the owner asked, and no `ready` has arrived yet."""
    predecessor_session_id: SessionId
    successor_session_id: SessionId | None
    project_id: ProjectId
    profile_id: ProfileId
    reason: str
    """`workflow` when a `ready` envelope opened it, `owner` when the owner asked first."""
    plan: str | None
    state: RolloverState
    failure_code: str | None
    failure_detail: str | None
    requested_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RolloverEvent:
    """One row of a rollover's history. `from_state` is None on the row that created it."""

    from_state: RolloverState | None
    to_state: RolloverState
    detail: str | None
    created_at: datetime


class RolloverStore(Protocol):
    """Rollovers in the domain store. Every write is one transaction with its history row."""

    async def open_for_ready(
        self,
        predecessor: SessionId,
        handoff_id: str,
        *,
        project_id: ProjectId,
        profile_id: ProfileId,
        plan: str | None,
        at: datetime,
    ) -> Rollover | None:
        """Open a HANDOFF_READY rollover for a `ready` envelope.

        Idempotent on `handoff_id`: a handoff already seen returns its row as it stands, in any
        state, so one envelope read on every pass opens one rollover. A REQUESTED row for this
        predecessor is advanced instead of opening another. None when the predecessor already
        has a different open rollover.
        """
        ...

    async def request(
        self,
        predecessor: SessionId,
        *,
        project_id: ProjectId,
        profile_id: ProfileId,
        at: datetime,
    ) -> Rollover | None:
        """Open a REQUESTED rollover for the owner's "Rollover now"; None when one is open."""
        ...

    async def advance(
        self,
        rollover_id: str,
        to_state: RolloverState,
        *,
        at: datetime,
        successor_session_id: SessionId | None = None,
        failure_code: str | None = None,
        failure_detail: str | None = None,
        detail: str | None = None,
    ) -> Rollover:
        """Move a rollover by the domain matrix and append the move to its history.

        A successor id, once recorded, is kept by later moves that pass None. Raises
        `IllegalRolloverMove` for a move the matrix refuses and `LookupError` for no such row.
        """
        ...

    async def get(self, rollover_id: str) -> Rollover | None: ...

    async def open_rollovers(self) -> tuple[Rollover, ...]:
        """Every rollover not in a terminal state, oldest request first."""
        ...

    async def events(self, rollover_id: str) -> Sequence[RolloverEvent]: ...

    async def continued_from(self, session_id: SessionId) -> SessionId | None:
        """The predecessor this session completed a rollover from, if any."""
        ...

    async def continued_as(self, session_id: SessionId) -> SessionId | None:
        """The successor this session completed a rollover to, if any."""
        ...
