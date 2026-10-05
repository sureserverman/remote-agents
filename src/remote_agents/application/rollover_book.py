"""Ask for, cancel and read rollovers -- what both surfaces call (DEC-046, DEC-115).

Neither surface launches, types or stops anything for a rollover. Asking writes a REQUESTED row
and nothing else; the pass in `serve` is what hands the request to the workflow and acts on
the `ready` the workflow writes back, so "Rollover now" never bypasses the workflow's own gate
(brief §32). A cancel is offered only before the workflow answered: from HANDOFF_READY on, a
successor may already be starting, and only the pass may end the rollover then.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from remote_agents.application.session_actions import (
    CANCEL_ROLLOVER,
    ROLLOVER,
    ROLLOVER_UNAVAILABLE,
    rollover_actions,
    rollover_outcome,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionRecord
from remote_agents.domain.rollover import RolloverState
from remote_agents.ports.rollover_setting import RolloverSettingPort
from remote_agents.ports.rollover_store import (
    IllegalRolloverMove,
    Rollover,
    RolloverEvent,
    RolloverStore,
)


class RolloverBook:
    """The rollover use cases both surfaces share, over the one domain store."""

    def __init__(
        self,
        store: RolloverStore,
        *,
        rollable: frozenset[ProfileId] = frozenset(),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        #: Which profiles roll over (`adapters.agents.registry.profiles_that_roll_over`),
        #: handed in by the composition because neither this layer nor a surface may name a
        #: provider. Empty offers Rollover now on nothing.
        self._rollable = rollable
        self._now = now

    @property
    def rollable(self) -> frozenset[ProfileId]:
        return self._rollable

    async def offered(
        self, record: SessionRecord, switch: RolloverSettingPort | None
    ) -> tuple[str, ...]:
        """`session_actions.rollover_actions` for `record`, with the switch and its open
        rollover read now -- the one read both surfaces draw their rollover rows from (DEC-007).
        No switch reads as off."""
        rollover = await self.open_for(record.session_id)
        return rollover_actions(
            record.state,
            record.profile_id,
            self._rollable,
            switch_on=await _switch_on(switch),
            open_state=None if rollover is None else rollover.state,
        )

    async def press(
        self, action: str, record: SessionRecord, switch: RolloverSettingPort | None
    ) -> str:
        """Perform a pressed rollover action on `record` and say what happened, in the words
        both surfaces show. Writes or withdraws at most one row; never launches, types or stops.

        Rollover now re-checks the policy at issue time (DEC-007) -- a button or key drawn
        before the switch went off, or the session stopped, asks for nothing. An open rollover
        is not part of that re-check: it is the store's own refusal, and says "already open".
        """
        if action == ROLLOVER:
            if ROLLOVER not in rollover_actions(
                record.state,
                record.profile_id,
                self._rollable,
                switch_on=await _switch_on(switch),
                open_state=None,
            ):
                return ROLLOVER_UNAVAILABLE
            asked = await self.request(
                record.session_id, project_id=record.project_id, profile_id=record.profile_id
            )
            return rollover_outcome(ROLLOVER, done=asked is not None)
        if action == CANCEL_ROLLOVER:
            return rollover_outcome(CANCEL_ROLLOVER, done=await self.cancel(record.session_id))
        raise ValueError(f"not a rollover action: {action!r}")

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
                rollover.id,
                RolloverState.CANCELLED,
                at=self._now(),
                detail="owner cancelled",
                # Only the request the owner saw: one the workflow answered since is the pass's.
                expected_from=RolloverState.REQUESTED,
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

    async def events(self, rollover_id: str) -> Sequence[RolloverEvent]:
        """A rollover's history, oldest first -- a row reads its notes (`waiting:`) from it."""
        return await self._store.events(rollover_id)

    async def latest_for(self, session_id: SessionId) -> Rollover | None:
        """`session_id`'s newest rollover that was not cancelled, open or ended -- what its row
        reports (`session_views.rollover_marks`)."""
        return await self._store.latest_for(session_id)

    async def continued_from(self, session_id: SessionId) -> SessionId | None:
        return await self._store.continued_from(session_id)

    async def continued_as(self, session_id: SessionId) -> SessionId | None:
        return await self._store.continued_as(session_id)


async def _switch_on(switch: RolloverSettingPort | None) -> bool:
    return False if switch is None else await switch.read()
