"""Durable rollovers: one row per rollover, an append-only history, and lineage read off both.

A rollover is its own object rather than fields on `SessionRecord`, whose rebuild is positional
(`domain/models.py`), so an appended field risks a silent mis-rebuild (DEC-115). Lineage --
"continued from" and "continued as" -- is therefore never stored on a session; it is derived
from COMPLETED rollovers, so a rollover that failed after launching a successor links nothing.

Rows live in the domain store so a change wakes the store watcher on both surfaces (DEC-090).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.domain.rollover import RolloverState
from remote_agents.ports.handoff_envelopes import HandoffDestination

REQUEST_WRITTEN = "request written"
"""The history detail `record_request` writes: the owner's request was handed to the workflow.
A row reads it to tell a written request from one still waiting for its checkout's slot."""

TRUST_HOLD = "trust-hold: successor on its folder-trust dialog"
"""The history detail that holds a rollover for its successor's folder trust (DEC-125). Its
presence is the durable fact that nothing was typed at the launch, the one case a later pass may
still type; a row reads it to say the rollover is waiting for trust. Written by
`record_successor` in the transaction that records the successor, so neither exists alone."""

TRUSTED_WAITING = "trusted: waiting for an idle predecessor"
"""The history detail a pass writes, once, on finding a trust-held successor trusted but its
predecessor working (DEC-125). Its time starts the wait for idle; a row reads it to say so."""


class IllegalRolloverMove(ValueError):
    """An `advance` the domain matrix does not allow; nothing was written."""


@dataclass(frozen=True, slots=True)
class ResolvedRolloverDestination:
    project_id: ProjectId
    destination: HandoffDestination


@dataclass(frozen=True, slots=True)
class RolloverTarget:
    """Immutable launch/adoption data resolved before any successor can start."""

    version: int
    project_id: ProjectId
    destination: HandoffDestination
    source_plan: str | None
    selection: tuple[bool, int | None, int | None] | None
    ready_sha256: str | None
    permissions: PermissionPreset | None

    def __post_init__(self) -> None:
        d = self.destination
        valid = (
            type(self.version) is int
            and self.version in (1, 2)
            and isinstance(d, HandoffDestination)
            and isinstance(d.root, Path)
            and d.root.is_absolute()
            and all(isinstance(v, str) for v in (d.branch, d.plan, d.locator))
            and (
                d.master_plan is None
                or (isinstance(d.master_plan, str) and d.master_plan.startswith("/"))
            )
            and (self.source_plan is None or isinstance(self.source_plan, str))
            and (self.permissions is None or isinstance(self.permissions, PermissionPreset))
        )
        if self.version == 2:
            valid = valid and (
                bool(d.branch)
                and d.plan.startswith("/")
                and re.fullmatch(r"Task \d+\.\d+", d.locator) is not None
                and isinstance(self.source_plan, str)
                and self.source_plan.startswith("/")
                and isinstance(self.ready_sha256, str)
                and re.fullmatch(r"[0-9a-f]{64}", self.ready_sha256) is not None
                and isinstance(self.selection, tuple)
                and len(self.selection) == 3
                and type(self.selection[0]) is bool
                and all(v is None or (type(v) is int and v > 0) for v in self.selection[1:])
            )
        elif self.version == 1:
            valid = valid and (
                self.source_plan is None
                and self.selection is None
                and self.ready_sha256 is None
                and d.master_plan is None
                and d.locator == ""
            )
        if not valid:
            raise ValueError("invalid stored rollover target")


@dataclass(frozen=True, slots=True)
class Rollover:
    """One stored rollover; project_id is the source, target.project_id the destination.

    `plan` is carried and shown, never opened or interpreted by the receiver.
    """

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
    source_root: Path | None = None
    target: RolloverTarget | None = None


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
        source_root: Path | None = None,
        target: RolloverTarget | None = None,
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

    async def freeze(
        self, rollover_id: str, source_root: Path, target: RolloverTarget | None, *, at: datetime
    ) -> Rollover:
        """Bind an unset source/target once, refusing later changes; safe for legacy holds."""
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
        self, rollover_id: str, successor: SessionId, *, at: datetime, held: bool = False
    ) -> Rollover:
        """Record the session a launch just started, while SUCCESSOR_STARTING, as its own
        history row. The launch mints the id, so it is known only after the state that licensed
        the launch is persisted. Idempotent for the same id; any other is `IllegalRolloverMove`.
        With `held`, a `TRUST_HOLD` row is appended in the same transaction: a successor up on
        its trust dialog is never recorded without the hold that says nothing was typed.
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

    async def latest_for(
        self, predecessor: SessionId, *, include_cancelled: bool = False
    ) -> Rollover | None:
        """The newest rollover of `predecessor` that was not cancelled, in any state -- the one
        whose FAILED or STOP_FAILED holds the next `ready`, so the one a row reports."""
        ...

    async def continued_from(self, session_id: SessionId) -> SessionId | None:
        """The predecessor this session completed a rollover from, if any."""
        ...

    async def continued_as(self, session_id: SessionId) -> SessionId | None:
        """The successor this session completed a rollover to, if any."""
        ...
