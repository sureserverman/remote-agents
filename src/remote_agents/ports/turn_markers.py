"""Whether a managed session's agent has a turn running, as its own hooks said (DEC-104).

One fact per session: "a turn started, at this time, by this agent". The agent's
`UserPromptSubmit` hook starts it; that same agent's `Stop` or `StopFailure` hook ends it; the
service ends it when the screen shows the turn over (an interrupt fires no hook) or the session
ends. Nothing the owner typed is part of it -- the store holds a name, a time and the agent's
own id for its session, and no content at all.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class TurnMarkers(Protocol):
    """Start, end and read the per-session "a turn started" marker; never raises."""

    def start(self, session_id: str, owner: str | None = None) -> None:
        """Mark a turn started now by agent `owner` -- refreshing the time of a marker already
        there, whose owner, if it has one, stays."""
        ...

    def end(self, session_id: str, owner: str | None = None) -> None:
        """Remove the session's marker; nothing when there is none. With `owner`, only when the
        marker records that owner or none."""
        ...

    def started_at(self, session_id: str) -> datetime | None:
        """When the session's running turn started, or None when no marker is there."""
        ...

    def sessions(self) -> tuple[str, ...]:
        """Every session holding a marker."""
        ...
