"""Where a notification that removes itself is remembered until it is removed.

Two kinds use it: an early limits reset, deleted after the owner's chosen lifetime, and a
failed rollover, deleted once resolved or after an hour. A message id held only in memory is
lost on every `serve` restart, and the service restarts several times on a working day, so the
message would stay in the chat for good. Kept in the UI store for DEC-090's reason: it is the
bot's own bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ExpiringNotice:
    """One sent message, what it was about, and when it is to be deleted at the latest.

    `subject` names what the message was about, in the kind's own terms: the provider for an
    early reset, the rollover id for a failed rollover. A newer message on the same subject
    replaces the older one, and a resolved subject removes its message early.
    """

    chat_id: int
    message_id: int
    kind: str
    subject: str
    expires_at: datetime


class ExpiringNoticeStore(Protocol):
    async def remember(self, notice: ExpiringNotice) -> None: ...

    async def standing(self, kind: str) -> tuple[ExpiringNotice, ...]:
        """Every remembered notice of `kind`, oldest first."""
        ...

    async def forget(self, chat_id: int, message_id: int) -> None: ...
