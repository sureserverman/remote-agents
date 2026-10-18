"""Messages that delete themselves: when they are due, when their news is over, or when a newer
message on the same subject replaces them.

Shared by the early-reset and failed-rollover notifiers, which `send_apart` their messages and
used to forget them on the spot -- so every one stayed in the chat until the owner deleted it
by hand. Each sent message is now remembered with its deadline (`ports.expiring_notices`), and
the notifier's own pass sweeps the ones that are due.

**Telegram lets a bot delete its own message for 48 hours**, so every lifetime here is shorter.
A delete Telegram refuses is not retried: it will not start working later. A delete that never
reached Telegram is, on the next sweep.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from remote_agents.adapters.telegram.flood import FloodGate
from remote_agents.ports.expiring_notices import ExpiringNotice, ExpiringNoticeStore

_LOG = logging.getLogger(__name__)


class InMemoryExpiringNoticeStore:
    """`ports.expiring_notices.ExpiringNoticeStore` for a composition with no UI store."""

    def __init__(self) -> None:
        self._notices: dict[tuple[int, int], ExpiringNotice] = {}

    async def remember(self, notice: ExpiringNotice) -> None:
        self._notices[(notice.chat_id, notice.message_id)] = notice

    async def standing(self, kind: str) -> tuple[ExpiringNotice, ...]:
        return tuple(notice for notice in self._notices.values() if notice.kind == kind)

    async def forget(self, chat_id: int, message_id: int) -> None:
        self._notices.pop((chat_id, message_id), None)


class ExpiringMessages:
    """One kind's sent messages, remembered until each is deleted."""

    def __init__(
        self,
        *,
        view: object,
        kind: str,
        store: ExpiringNoticeStore | None = None,
        flood: FloodGate | None = None,
    ) -> None:
        self._view = view
        self._kind = kind
        self._store = store if store is not None else InMemoryExpiringNoticeStore()
        #: The chat-wide "not yet" the senders share: a delete is a request too, and one sent
        #: into a flood hold extends the ban for every sender.
        self._flood = flood if flood is not None else FloodGate()

    async def sent(
        self, bot: object, message_id: int, *, subject: str, expires_at: datetime
    ) -> None:
        """Remember a message just sent, then delete the earlier ones on the same subject.

        Remembered first, so a delete that fails right after a send -- when a flood hold is
        likeliest -- cannot cost the new message its own removal. A replaced message that could
        not be deleted stays remembered and goes on a later sweep. Never raises.
        """
        try:
            earlier = [
                notice
                for notice in await self._store.standing(self._kind)
                if notice.subject == subject
            ]
            await self._store.remember(
                ExpiringNotice(self._chat_id(), message_id, self._kind, subject, expires_at)
            )
        except Exception:
            _LOG.warning(
                "a %s message (%d) could not be remembered; it will not delete itself",
                self._kind,
                message_id,
                exc_info=True,
            )
            return
        for notice in earlier:
            if self._flood.held():
                return
            try:
                await self._delete(bot, notice)
            except Exception:
                _LOG.debug(
                    "the replaced %s message %d is left for the next sweep",
                    self._kind,
                    notice.message_id,
                    exc_info=True,
                )

    async def sweep(
        self,
        bot: object,
        now: datetime,
        *,
        settled: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        """Delete every message that is due, or whose subject `settled` says is over.

        Never raises. A message that could not be checked or deleted this time stays
        remembered and is tried on the next sweep, as is everything while the chat is under a
        flood hold.
        """
        if self._flood.held():
            return
        try:
            standing = await self._store.standing(self._kind)
        except Exception:
            _LOG.debug("the %s messages could not be read", self._kind, exc_info=True)
            return
        for notice in standing:
            if notice.chat_id != self._chat_id():
                # Remembered in a chat this bot no longer speaks in. Message ids are per chat,
                # so deleting this id here would remove some other message; forget it instead.
                await self._store.forget(notice.chat_id, notice.message_id)
                continue
            try:
                due = notice.expires_at <= now
                if not due and settled is not None:
                    due = await settled(notice.subject)
                if due:
                    await self._delete(bot, notice)
            except Exception:
                _LOG.debug(
                    "the %s message %d could not be removed this pass",
                    self._kind,
                    notice.message_id,
                    exc_info=True,
                )

    def _chat_id(self) -> int:
        return int(getattr(self._view, "chat_id", 0))

    async def _delete(self, bot: object, notice: ExpiringNotice) -> None:
        """Delete one message and forget it; a refusal is forgotten too, with a log line.

        `discard` answers `False` only for a delete Telegram refused, which does not start
        working later, so the row goes either way. A transport error raises and keeps it.
        """
        removed = await self._view.discard(bot, notice.message_id)  # type: ignore[attr-defined]
        if not removed:
            _LOG.info(
                "the %s message %d could not be deleted; it stays in the chat",
                self._kind,
                notice.message_id,
            )
        await self._store.forget(notice.chat_id, notice.message_id)
