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

    def end(self, session_id: str) -> None:
        """Remove the session's marker, whoever owns it; nothing when there is none. The
        service's end: the screen showed the turn over, or the session ended."""
        ...

    def end_if_owned_by(self, session_id: str, owner: object) -> None:
        """A hook's end: remove the marker only when it records `owner`, or no owner at all. An
        `owner` that is not a usable id ends no marker that has one."""
        ...

    def owner(self, session_id: str) -> str | None:
        """The agent id the session's marker records, or None: no marker, or none recorded."""
        ...

    def started_at(self, session_id: str) -> datetime | None:
        """When the session's running turn started, or None when no marker is there."""
        ...

    def sessions(self) -> tuple[str, ...]:
        """Every session holding a marker."""
        ...

    def listable(self) -> bool:
        """False when the markers' directory exists and cannot be listed. `sessions` then
        answers empty, which a caller deciding something on it must not read as "none"."""
        ...


class ConversationMarks(Protocol):
    """Each session's latest own conversation, as its agent named it at a turn's start;
    never raises.

    Written by the hook, read by the resume after a restart. It holds an identifier and
    nothing typed (DEC-067: admitted because the resume reopens it). It is replaced whole, so
    the newest write wins with no clock, and it survives a reboot with no drain."""

    def record(self, session_id: str, conversation_id: str) -> None:
        """Make `conversation_id` the session's latest; a malformed id is never written."""
        ...

    def latest(self, session_id: str) -> str | None:
        """The session's latest conversation, or None when none is recorded or it is malformed."""
        ...

    def sessions(self) -> tuple[str, ...]:
        """Every session holding a mark."""
        ...

    def listable(self) -> bool:
        """False when the marks' directory exists and cannot be listed: `sessions` then
        answers empty, and the resume after a restart must not read that as "no marks"."""
        ...

    def forget(self, session_id: str) -> None:
        """Remove the session's mark, once nothing will resume it."""
        ...
