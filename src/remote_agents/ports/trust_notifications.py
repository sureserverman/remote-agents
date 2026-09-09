"""Where the one standing folder-trust question per session is remembered."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from remote_agents.domain.models import SessionId


@dataclass(frozen=True, slots=True)
class StandingTrustQuestion:
    """A trust question that has been asked in a chat, and whether it has been answered.

    `settled` rather than the row's absence, because the two are not the same fact and the
    difference decides what the next pass does. DEC-034 amends a notification in place, so the
    message has to still be nameable after the answer — a deleted row and a never-asked
    session are one absence, and a pass reading it would send the question again.
    """

    session_id: SessionId
    chat_id: int
    message_id: int
    settled: bool


class TrustNotificationStore(Protocol):
    async def remember(self, session_id: SessionId, *, chat_id: int, message_id: int) -> None: ...
    async def standing_for(self, session_id: SessionId) -> StandingTrustQuestion | None: ...
    async def unsettled(self) -> tuple[StandingTrustQuestion, ...]: ...
    async def settle(self, session_id: SessionId) -> None: ...
