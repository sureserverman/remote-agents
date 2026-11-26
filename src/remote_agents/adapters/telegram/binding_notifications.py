"""The one message for a session that could not bind its thread, or be given its permissions.

The restart notice's shape (`restart_notifications.py`): sent apart from the live view, at once
when the bot is attached, else held until it attaches, and given up with one journal line after
`_GIVE_UP_AFTER`. **Escaped here, once** (DEC-014): the project's name reaches a surface that
parses HTML. The pass tells once per session, so nothing here counts.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape

from telegram.constants import ParseMode

from remote_agents.adapters.telegram.expiring_notices import ExpiringMessages
from remote_agents.adapters.telegram.flood import FloodGate
from remote_agents.application.permission_apply import permissions_notice
from remote_agents.application.thread_binding import unbound_notice
from remote_agents.domain.models import SessionRecord
from remote_agents.ports.expiring_notices import ExpiringNoticeStore

_LOG = logging.getLogger(__name__)

#: How long a refused notice is tried again before it is given up, as the rollovers' are.
_GIVE_UP_AFTER = timedelta(minutes=10)


@dataclass(frozen=True)
class _Warning:
    kind: str
    session_id: str
    text: str


class BindingNotifier:
    """Send each unbound notice apart from the live view, holding it until it is delivered."""

    def __init__(
        self,
        *,
        view: object,
        project_name: Callable[[str], str],
        flood: FloodGate | None = None,
        notices: ExpiringNoticeStore | None = None,
        finished: Callable[[tuple[str, ...]], Awaitable[tuple[str, ...]]] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._view = view
        self._project_name = project_name
        self._flood = flood if flood is not None else FloodGate()
        self._now = now
        self._bot: object | None = None
        self._pending: list[_Warning] = []
        self._finished = finished
        self._messages = {
            kind: ExpiringMessages(view=view, kind=kind, store=notices, flood=self._flood)
            for kind in ("binding", "permissions")
        }
        self._refused_since: datetime | None = None
        self._sending = asyncio.Lock()

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(item.text for item in self._pending)

    def attach(self, bot: object) -> None:
        self._bot = bot

    async def notify(self, record: SessionRecord) -> None:
        self._pending.append(
            _Warning(
                "binding",
                str(record.session_id),
                escape(unbound_notice(record, self._project_name(str(record.project_id)))),
            )
        )
        await self.pass_once()

    async def notify_permissions(self, record: SessionRecord, reason: str) -> None:
        """The same queue, so the two notices about one session keep their order. The reason
        is the daemon's words in part, and is escaped with the rest (DEC-014)."""
        self._pending.append(
            _Warning(
                "permissions",
                str(record.session_id),
                escape(
                    permissions_notice(record, self._project_name(str(record.project_id)), reason)
                ),
            )
        )
        await self.pass_once()

    async def _settled(self, session_id: str) -> bool:
        return self._finished is not None and session_id in await self._finished((session_id,))

    async def pass_once(self) -> None:
        """Send what is outstanding, stopping at a flood hold or a refusal."""
        if self._bot is None:
            return
        async with self._sending:
            if self._finished is not None:
                for messages in self._messages.values():
                    await messages.sweep(self._bot, self._now(), settled=self._settled)
            while self._pending:
                if self._flood.held():
                    return
                item = self._pending[0]
                try:
                    obsolete = await self._settled(item.session_id)
                except Exception:
                    _LOG.warning("holding a session warning until its lifecycle can be read")
                    return
                if obsolete:
                    self._pending.pop(0)
                    self._refused_since = None
                    continue
                try:
                    message_id = await self._view.send_apart(  # type: ignore[attr-defined]
                        self._bot, {"text": item.text, "parse_mode": ParseMode.HTML}
                    )
                except Exception:
                    now = self._now()
                    self._refused_since = self._refused_since or now
                    if now - self._refused_since >= _GIVE_UP_AFTER:
                        _LOG.warning("an unbound notice was refused for 10 minutes; dropped")
                        self._pending.pop(0)
                        self._refused_since = None
                    return
                self._pending.pop(0)
                self._refused_since = None
                if self._finished is not None:
                    await self._messages[item.kind].sent(
                        self._bot,
                        message_id,
                        subject=item.session_id,
                        expires_at=datetime.max.replace(tzinfo=UTC),
                    )
                    await self._messages[item.kind].sweep(
                        self._bot, self._now(), settled=self._settled
                    )
