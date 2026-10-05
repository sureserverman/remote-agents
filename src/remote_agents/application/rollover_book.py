"""Ask for, cancel and read rollovers -- what both surfaces call (DEC-046, DEC-115).

Neither surface launches, types or stops anything for a rollover. Asking writes a REQUESTED row
and nothing else; the pass in `serve` is what hands the request to the workflow and acts on
the `ready` the workflow writes back, so "Rollover now" never bypasses the workflow's own gate
(brief §32). A cancel is offered only before the workflow answered: from HANDOFF_READY on, a
successor may already be starting, and only the pass may end the rollover then.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.rollover import RolloverState
from remote_agents.ports.rollover_store import IllegalRolloverMove, Rollover, RolloverStore


class RolloverBook:
    """The rollover use cases both surfaces share, over the one domain store."""

    def __init__(
        self,
        store: RolloverStore,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._now = now

    async def request(
        self, session_id: SessionId, *, project_id: ProjectId, profile_id: ProfileId
    ) -> Rollover | None:
        """Ask for `session_id` to be rolled over at its workflow's next gate. None when it
        already has an open rollover: one at a time per session, whoever opened it."""
        return await self._store.request(
            session_id, project_id=project_id, profile_id=profile_id, at=self._now()
        )

    async def cancel(self, session_id: SessionId) -> bool:
        """Withdraw `session_id`'s request while it is still only a request. False when there
        is none to withdraw -- no open rollover, or one the workflow has already answered."""
        rollover = await self.open_for(session_id)
        if rollover is None or rollover.state is not RolloverState.REQUESTED:
            return False
        try:
            await self._store.advance(
                rollover.id, RolloverState.CANCELLED, at=self._now(), detail="owner cancelled"
            )
        except IllegalRolloverMove:
            # The pass moved it first (the `ready` arrived): the rollover is under way, and the
            # cancel has nothing left to withdraw.
            return False
        return True

    async def open_for(self, session_id: SessionId) -> Rollover | None:
        """The open rollover whose predecessor is `session_id`, if any."""
        for rollover in await self._store.open_rollovers():
            if rollover.predecessor_session_id == session_id:
                return rollover
        return None

    async def open_rollovers(self) -> tuple[Rollover, ...]:
        return await self._store.open_rollovers()

    async def continued_from(self, session_id: SessionId) -> SessionId | None:
        return await self._store.continued_from(session_id)

    async def continued_as(self, session_id: SessionId) -> SessionId | None:
        return await self._store.continued_as(session_id)
