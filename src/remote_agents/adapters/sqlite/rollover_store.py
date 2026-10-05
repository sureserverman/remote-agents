"""Rollovers in the domain store (migration 18), so a change wakes the store watcher (DEC-090).

Each write is one transaction holding the row change and its history row, so a crash leaves
either both or neither. The history is append-only: triggers refuse UPDATE and DELETE (DEC-022).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.rollover import NEEDS_SUCCESSOR, TERMINAL, RolloverState, is_legal
from remote_agents.ports.rollover_store import IllegalRolloverMove, Rollover, RolloverEvent

_COLUMNS = (
    "rollover_id, handoff_id, predecessor_session_id, successor_session_id, project_id,"
    " profile_id, reason, plan, state, failure_code, failure_detail, requested_at, updated_at"
)
_OPEN = "state NOT IN ({})".format(", ".join(f"'{state.value}'" for state in sorted(TERMINAL)))
_HOLDING = (RolloverState.FAILED, RolloverState.STOP_FAILED)
"""A predecessor whose latest rollover ended in one of these opens no more from a `ready` until
the owner asks: a forged envelope costs one extra session, never a stream of them (DEC-115)."""
_REQUEST_WRITTEN = "request written"
"""The history detail marking that the owner's request was handed to the workflow."""


class SQLiteRolloverStore:
    """`RolloverStore` over the domain database the session store writes."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

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
        seen = self._select("handoff_id = ?", (handoff_id,))
        if seen:
            return self._own(seen[0], predecessor, project_id)
        try:
            open_rows = self._select(f"predecessor_session_id = ? AND {_OPEN}", (str(predecessor),))
            if not open_rows and self._held(predecessor):
                return None
            if open_rows:
                (current,) = open_rows
                if current.handoff_id == handoff_id:
                    return current
                if current.state is not RolloverState.REQUESTED:
                    return None
                with self._connection:
                    self._connection.execute(
                        "UPDATE rollovers SET handoff_id = ?, plan = ? WHERE rollover_id = ?",
                        (handoff_id, plan, current.id),
                    )
                    return self._move(current, RolloverState.HANDOFF_READY, at=at)
            return self._insert(
                predecessor,
                handoff_id=handoff_id,
                project_id=project_id,
                profile_id=profile_id,
                reason="workflow",
                plan=plan,
                state=RolloverState.HANDOFF_READY,
                at=at,
            )
        except (sqlite3.IntegrityError, IllegalRolloverMove):
            # Another writer took this handoff, or moved the REQUESTED row, between the reads
            # above and the write: the handoff's own row, if it now exists, is the answer.
            seen = self._select("handoff_id = ?", (handoff_id,))
            return self._own(seen[0], predecessor, project_id) if seen else None

    async def record_successor(
        self, rollover_id: str, successor: SessionId, *, at: datetime
    ) -> Rollover:
        current = await self.get(rollover_id)
        if current is None:
            raise LookupError(f"no rollover {rollover_id}")
        if current.state is not RolloverState.SUCCESSOR_STARTING:
            raise IllegalRolloverMove(
                f"a successor is recorded while starting, not {current.state}"
            )
        if current.successor_session_id == successor:
            return current
        if current.successor_session_id is not None:
            raise IllegalRolloverMove("a recorded successor is never replaced")
        if successor == current.predecessor_session_id:
            raise IllegalRolloverMove("a session does not succeed itself")
        stamp = _stored(at)
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE rollovers SET successor_session_id = ?, updated_at = ?"
                " WHERE rollover_id = ? AND state = ? AND successor_session_id IS NULL",
                (str(successor), stamp, rollover_id, current.state.value),
            )
            if cursor.rowcount != 1:
                raise IllegalRolloverMove("the rollover changed under this record")
            self._append(
                rollover_id, current.state, current.state, None, "successor recorded", stamp
            )
        return self._select("rollover_id = ?", (rollover_id,))[0]

    async def request(
        self,
        predecessor: SessionId,
        *,
        project_id: ProjectId,
        profile_id: ProfileId,
        at: datetime,
    ) -> Rollover | None:
        return self._insert(
            predecessor,
            handoff_id=None,
            project_id=project_id,
            profile_id=profile_id,
            reason="owner",
            plan=None,
            state=RolloverState.REQUESTED,
            at=at,
        )

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
        current = await self.get(rollover_id)
        if current is None:
            raise LookupError(f"no rollover {rollover_id}")
        with self._connection:
            return self._move(
                current,
                to_state,
                at=at,
                successor_session_id=successor_session_id,
                failure_code=failure_code,
                failure_detail=failure_detail,
                detail=detail,
            )

    async def record_request(self, rollover_id: str, *, at: datetime) -> bool:
        stamp = _stored(at)
        with self._connection:
            # One transaction: the state test and the "already recorded" test are read under the
            # same write as the row they guard, so two passes cannot both see it unrecorded.
            row = self._connection.execute(
                "SELECT state FROM rollovers WHERE rollover_id = ?", (rollover_id,)
            ).fetchone()
            if row is None or RolloverState(row[0]) is not RolloverState.REQUESTED:
                return False
            if self._connection.execute(
                "SELECT 1 FROM rollover_events WHERE rollover_id = ? AND detail = ?",
                (rollover_id, _REQUEST_WRITTEN),
            ).fetchone():
                return False
            self._append(
                rollover_id,
                RolloverState.REQUESTED,
                RolloverState.REQUESTED,
                None,
                _REQUEST_WRITTEN,
                stamp,
            )
        return True

    async def note(self, rollover_id: str, detail: str, *, at: datetime) -> None:
        with self._connection:
            row = self._connection.execute(
                "SELECT state FROM rollovers WHERE rollover_id = ?", (rollover_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"no rollover {rollover_id}")
            state = RolloverState(row[0])
            self._append(rollover_id, state, state, None, detail, _stored(at))

    async def get(self, rollover_id: str) -> Rollover | None:
        rows = self._select("rollover_id = ?", (rollover_id,))
        return rows[0] if rows else None

    async def open_rollovers(self) -> tuple[Rollover, ...]:
        return self._select(_OPEN, ())

    async def events(self, rollover_id: str) -> Sequence[RolloverEvent]:
        rows = self._connection.execute(
            "SELECT from_state, to_state, failure_code, detail, created_at FROM rollover_events"
            " WHERE rollover_id = ? ORDER BY event_id",
            (rollover_id,),
        ).fetchall()
        return tuple(
            RolloverEvent(
                from_state=None if from_state is None else RolloverState(from_state),
                to_state=RolloverState(to_state),
                failure_code=failure_code,
                detail=detail,
                created_at=datetime.fromisoformat(created_at),
            )
            for from_state, to_state, failure_code, detail, created_at in rows
        )

    def _own(self, row: Rollover, predecessor: SessionId, project_id: ProjectId) -> Rollover | None:
        """The row for a handoff id, only when it is this predecessor's in this project: an id
        planted in another checkout must not hand back -- or advance -- someone else's."""
        if row.predecessor_session_id != predecessor or row.project_id != project_id:
            return None
        return row

    def _held(self, predecessor: SessionId) -> bool:
        latest = self._connection.execute(
            "SELECT state FROM rollovers WHERE predecessor_session_id = ?"
            " ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (str(predecessor),),
        ).fetchone()
        return latest is not None and RolloverState(latest[0]) in _HOLDING

    async def continued_from(self, session_id: SessionId) -> SessionId | None:
        return self._lineage("predecessor_session_id", "successor_session_id", session_id)

    async def continued_as(self, session_id: SessionId) -> SessionId | None:
        return self._lineage("successor_session_id", "predecessor_session_id", session_id)

    def _lineage(self, wanted: str, keyed: str, session_id: SessionId) -> SessionId | None:
        row = self._connection.execute(
            f"SELECT {wanted} FROM rollovers WHERE {keyed} = ? AND state = ?"
            " ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (str(session_id), RolloverState.COMPLETED.value),
        ).fetchone()
        return None if row is None or row[0] is None else SessionId.parse(row[0])

    def _insert(
        self,
        predecessor: SessionId,
        *,
        handoff_id: str | None,
        project_id: ProjectId,
        profile_id: ProfileId,
        reason: str,
        plan: str | None,
        state: RolloverState,
        at: datetime,
    ) -> Rollover | None:
        rollover_id = uuid4().hex
        stamp = _stored(at)
        try:
            with self._connection:
                self._connection.execute(
                    f"INSERT INTO rollovers({_COLUMNS})"
                    " VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                    (
                        rollover_id,
                        handoff_id,
                        str(predecessor),
                        str(project_id),
                        str(profile_id),
                        reason,
                        plan,
                        state.value,
                        stamp,
                        stamp,
                    ),
                )
                self._append(rollover_id, None, state, None, None, stamp)
        except sqlite3.IntegrityError as error:
            # The partial index refused a second open rollover for this predecessor. A taken
            # handoff id is re-raised for `open_for_ready` to resolve; anything else is a bug.
            if "rollovers.predecessor_session_id" in str(error):
                return None
            raise
        return self._select("rollover_id = ?", (rollover_id,))[0]

    def _move(
        self,
        current: Rollover,
        to_state: RolloverState,
        *,
        at: datetime,
        successor_session_id: SessionId | None = None,
        failure_code: str | None = None,
        failure_detail: str | None = None,
        detail: str | None = None,
    ) -> Rollover:
        """Write one legal move inside the caller's transaction."""
        if not is_legal(current.state, to_state):
            raise IllegalRolloverMove(f"{current.state.value} -> {to_state.value}")
        if (
            successor_session_id is not None
            and current.successor_session_id is not None
            and successor_session_id != current.successor_session_id
        ):
            raise IllegalRolloverMove("a recorded successor is never replaced")
        successor = successor_session_id or current.successor_session_id
        if to_state in NEEDS_SUCCESSOR and successor is None:
            raise IllegalRolloverMove(f"{to_state.value} needs a recorded successor")
        if successor == current.predecessor_session_id:
            raise IllegalRolloverMove("a session does not succeed itself")
        stamp = _stored(at)
        cursor = self._connection.execute(
            "UPDATE rollovers SET state = ?, updated_at = ?,"
            " successor_session_id = COALESCE(?, successor_session_id),"
            " failure_code = COALESCE(?, failure_code),"
            " failure_detail = COALESCE(?, failure_detail)"
            " WHERE rollover_id = ? AND state = ?",
            (
                to_state.value,
                stamp,
                None if successor_session_id is None else str(successor_session_id),
                failure_code,
                failure_detail,
                current.id,
                current.state.value,
            ),
        )
        if cursor.rowcount != 1:
            # Another writer moved it since `current` was read; its move stands, this one is
            # refused rather than written over it.
            raise IllegalRolloverMove(f"{current.state.value} changed under this move")
        self._append(current.id, current.state, to_state, failure_code, detail, stamp)
        return self._select("rollover_id = ?", (current.id,))[0]

    def _append(
        self,
        rollover_id: str,
        from_state: RolloverState | None,
        to_state: RolloverState,
        failure_code: str | None,
        detail: str | None,
        stamp: str,
    ) -> None:
        self._connection.execute(
            "INSERT INTO rollover_events("
            "rollover_id, from_state, to_state, failure_code, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                rollover_id,
                None if from_state is None else from_state.value,
                to_state.value,
                failure_code,
                detail,
                stamp,
            ),
        )

    def _select(self, where: str, params: tuple) -> tuple[Rollover, ...]:
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM rollovers WHERE {where} ORDER BY requested_at, rollover_id",
            params,
        ).fetchall()
        return tuple(_rollover(row) for row in rows)


def _rollover(row: tuple) -> Rollover:
    (
        rollover_id,
        handoff_id,
        predecessor,
        successor,
        project_id,
        profile_id,
        reason,
        plan,
        state,
        failure_code,
        failure_detail,
        requested_at,
        updated_at,
    ) = row
    return Rollover(
        id=rollover_id,
        handoff_id=handoff_id,
        predecessor_session_id=SessionId.parse(predecessor),
        successor_session_id=None if successor is None else SessionId.parse(successor),
        project_id=ProjectId(project_id),
        profile_id=ProfileId(profile_id),
        reason=reason,
        plan=plan,
        state=RolloverState(state),
        failure_code=failure_code,
        failure_detail=failure_detail,
        requested_at=datetime.fromisoformat(requested_at),
        updated_at=datetime.fromisoformat(updated_at),
    )


def _stored(value: datetime) -> str:
    """Fixed-width UTC text, as the schedule store writes it. A naive value is refused."""
    if value.tzinfo is None:
        raise ValueError("a rollover instant must carry its zone")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
