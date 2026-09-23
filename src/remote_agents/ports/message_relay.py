"""What a surface may ask of the relay that types an owner's message into a session (DEC-099).

A port of its own, named for *messages*, because the Telegram adapter may not name the thing it
relays (`tests/architecture/check_telegram_actions.py` forbids the token, DEC-075) -- and a
surface should depend on what it may ask, not on the application class that answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from remote_agents.domain.models import SessionId
from remote_agents.ports.queued_prompts import QueuedPrompt
from remote_agents.ports.terminal import PromptReason


class RelayOutcome(StrEnum):
    """What became of a message the owner sent to a session."""

    SENT = "sent"
    QUEUED = "queued"
    """Waiting for the session's next "finished" event; `reason` says why it could not go now."""
    REFUSED = "refused"
    UNCONFIRMED = "unconfirmed"
    """Pasted, and not seen to land. Never retried."""


@dataclass(frozen=True, slots=True)
class RelayResult:
    outcome: RelayOutcome
    reason: PromptReason | None = None
    replaced: bool = False
    """For QUEUED: an earlier waiting message was replaced by this one."""


class MessageRelay(Protocol):
    async def submit(self, session_id: SessionId, text: str) -> RelayResult: ...

    async def retry(self, session_id: SessionId) -> RelayResult | None:
        """Deliver the session's waiting message after a "finished" event; None if none waits."""
        ...

    def cancel(self, session_id: SessionId) -> bool: ...

    def pending(self, session_id: SessionId) -> QueuedPrompt | None: ...

    async def sweep(self) -> None:
        """Drop waiting messages whose session is no longer running."""
        ...
