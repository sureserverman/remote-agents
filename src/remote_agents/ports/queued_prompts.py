"""The one relayed message a busy session may have waiting (DEC-099)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class QueuedPrompt:
    """An owner's message waiting for its session's next "finished" event."""

    session_id: str
    text: str
    queued_at: datetime
    claimed_at: datetime | None = None
    """Set on a claimed message: which claim this is, so only that claim can restore or settle
    it -- never a claimer that stalled past abandonment."""


class QueuedPromptStore(Protocol):
    """At most one waiting message per session; the newest one is the one that waits."""

    def queue(self, session_id: str, text: str) -> bool:
        """Store the message, replacing any older one; True when one was replaced."""
        ...

    def pending(self, session_id: str) -> QueuedPrompt | None: ...

    def claim(self, session_id: str) -> QueuedPrompt | None:
        """Mark the waiting message in flight and return it, so it is delivered once."""
        ...

    def restore(self, prompt: QueuedPrompt) -> bool:
        """Un-mark a claimed message after a refused delivery -- False if it was cancelled or
        replaced meanwhile."""
        ...

    def settle(self, prompt: QueuedPrompt) -> bool:
        """Remove a claimed message whose delivery is over, leaving any newer one waiting --
        False when the claim was already gone (cancelled or replaced meanwhile)."""
        ...

    def waiting(self) -> tuple[QueuedPrompt, ...]:
        """Every waiting message, for the sweep that clears sessions that stopped or ended."""
        ...

    def cancel(self, session_id: str) -> bool:
        """Remove the waiting message at the owner's request; True when there was one."""
        ...

    def clear(self, session_id: str) -> None:
        """Remove the waiting message because its session stopped or ended."""
        ...
