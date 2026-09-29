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

    async def record(self, stop: LimitStop, outcome: str, *, decided_at: datetime) -> None:
        """Write the stop's outcome. The first one written stands; a second is ignored."""
        ...

    async def last_resumed_at(self, session_id: str) -> datetime | None:
        """When this session was last nudged (`RESUMED`), or `None` if it never was."""
        ...
