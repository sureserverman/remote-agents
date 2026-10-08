"""What became of each limit stop, so a lift is acted on once -- across a restart too.

A limit never ends a session: the agent sits idle in its pane, and nothing it does afterwards
says the stop is over. So once the service has decided a stop lifted, and acted on it, that
decision is written down against the stop itself. Without it, Cursor -- which reports nothing
after a nudge -- would read "limit stop" as its newest news on every pass, and be acted on again
every thirty seconds.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from remote_agents.ports.agent_activity import LimitHit

LIFTED = "lifted"
"""The stop's window lifted and its line was retired from the bot's message."""

RESUMED = "resumed"
"""The stop lifted and the service's one nudge was typed into the idle composer."""

NUDGING = "nudging"
"""Written just before the nudge is typed, and upgraded to the final outcome once it is known.

Not an outcome: an intent. A stop found holding it by a process that did not write it was
interrupted between the typing and the record, so whether the text landed is unknown -- and it
is given up as unconfirmed, never typed again (DEC-099: a double submit is worse than a lost one).
"""

NOT_RESUMED = "not_resumed"
"""The prefix of `not_resumed:<reason>`: the stop lifted and the nudge could not be sent."""


@dataclass(frozen=True, slots=True)
class LimitStop:
    """One recorded limit stop that is still its session's newest news, and still undecided."""

    session_id: str
    stopped_at: datetime
    hit: LimitHit
    stamp: str
    """The stop's instant exactly as the store wrote it, which is what its outcome is keyed on:
    re-serialising `stopped_at` would miss the join for any row written in another format."""


class LimitStopOutcomes(Protocol):
    async def unresolved(self, session_ids: Collection[str]) -> tuple[LimitStop, ...]:
        """Each named session whose newest observation is a limit stop with no outcome yet."""
        ...

    async def record(self, stop: LimitStop, outcome: str, *, decided_at: datetime) -> bool:
        """Write the stop's outcome, answering whether it did. The first one written stands and
        a second is ignored -- except over `NUDGING`, which is an intent and is replaced by the
        outcome it became."""
        ...

    async def claim(self, stop: LimitStop, *, decided_at: datetime) -> bool:
        """Write `NUDGING` for the stop, answering whether it did: never over any row, so a
        stop already decided -- by a step that finished after this pass read it -- is not
        typed into again.

        An intent only ever loses a nudge, never doubles one: a process that stops after the
        claim and before the typing leaves an intent that is given up as unconfirmed."""
        ...

    async def release(self, stop: LimitStop) -> None:
        """Remove the stop's `NUDGING` intent -- nothing was typed -- and nothing else."""
        ...

    async def interrupted(self) -> tuple[LimitStop, ...]:
        """Every stop still holding a `NUDGING` intent."""
        ...

    async def last_resumed_at(self, session_id: str) -> datetime | None:
        """When this session was last nudged (`RESUMED`), or `None` if it never was."""
        ...

    async def prune(self, before: datetime) -> int:
        """Delete finished outcomes decided before `before`, answering how many went.

        Never a `NUDGING` intent, and never the outcome of a session that can still act: only
        sessions in a terminal lifecycle state, or no longer recorded at all, lose theirs."""
        ...
