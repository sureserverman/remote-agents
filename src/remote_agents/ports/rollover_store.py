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
    """One row of a rollover's history. `from_state` is None on the row that created it, and
    equals `to_state` on a row that recorded something without a move (the successor)."""

    from_state: RolloverState | None
    to_state: RolloverState
    failure_code: str | None
    """The move's own cause, kept on the history row too, so the audit names it (DEC-022)."""
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
        state, so one envelope read on every pass opens one rollover -- but only to the
        predecessor and project it was opened for; any other caller gets None. A REQUESTED row
        for this predecessor is advanced instead of opening another. None when the predecessor
        already has a different open rollover, and None when its latest rollover FAILED or
        STOP_FAILED and the owner has not asked again: then a `ready` waits for the owner.
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
        expected_from: RolloverState | None = None,
    ) -> Rollover:
        """Move a rollover by the domain matrix and append the move to its history.

        `expected_from` makes the move conditional on the state the caller read: a row moved
        since by another writer -- the pass in `serve`, while a surface acts -- is refused with
        `IllegalRolloverMove` rather than moved from wherever it now stands.

        A successor id, once recorded, is kept by later moves that pass None and never replaced.
        From ADOPTING on a successor must be recorded, and it is never the predecessor. Raises
        `IllegalRolloverMove` for a move these rules or the matrix refuse, and `LookupError`
        for no such row.
        """
        ...

    async def record_successor(
        self, rollover_id: str, successor: SessionId, *, at: datetime
    ) -> Rollover:
        """Record the session a launch just started, while SUCCESSOR_STARTING, as its own
        history row. The launch mints the id, so it is known only after the state that licensed
        the launch is persisted. Idempotent for the same id; any other is `IllegalRolloverMove`.
        """
        ...

    async def record_request(self, rollover_id: str, *, at: datetime) -> bool:
        """Record, while REQUESTED, that the owner's request is about to be written, as its own
        history row. True the first time only: the request is written once, after this record,
        so a restart finds it recorded and never asks again (DEC-004). False in any other state.
        """
        ...

    async def note(self, rollover_id: str, detail: str, *, at: datetime) -> None:
        """Append a history row recording something that happened without a move -- a restart
        finding the rollover where it stands. Nothing else changes; `LookupError` for no row."""
        ...

    async def get(self, rollover_id: str) -> Rollover | None: ...

    async def open_rollovers(self) -> tuple[Rollover, ...]:
        """Every rollover not in a terminal state, oldest request first."""
        ...

    async def events(self, rollover_id: str) -> Sequence[RolloverEvent]: ...

    async def latest_for(self, predecessor: SessionId) -> Rollover | None:
        """The newest rollover of `predecessor` that was not cancelled, in any state -- the one
        whose FAILED or STOP_FAILED holds the next `ready`, so the one a row reports."""
        ...

    async def continued_from(self, session_id: SessionId) -> SessionId | None:
        """The predecessor this session completed a rollover from, if any."""
        ...

    async def continued_as(self, session_id: SessionId) -> SessionId | None:
        """The successor this session completed a rollover to, if any."""
        ...
