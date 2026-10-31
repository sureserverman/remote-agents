"""The one message for a session that could not bind its thread.

The restart notice's shape (`restart_notifications.py`): sent apart from the live view, at once
when the bot is attached, else held until it attaches, and given up with one journal line after
`_GIVE_UP_AFTER`. **Escaped here, once** (DEC-014): the project's name reaches a surface that
parses HTML. The pass tells once per session, so nothing here counts.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from html import escape

from telegram.constants import ParseMode

from remote_agents.adapters.telegram.flood import FloodGate
from remote_agents.application.thread_binding import unbound_notice
from remote_agents.domain.models import SessionRecord

_LOG = logging.getLogger(__name__)

#: How long a refused notice is tried again before it is given up, as the rollovers' are.
_GIVE_UP_AFTER = timedelta(minutes=10)


class BindingNotifier:
    """Send each unbound notice apart from the live view, holding it until it is delivered."""

    def __init__(
        self,
        *,
        view: object,
        project_name: Callable[[str], str],
        flood: FloodGate | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._view = view
        self._project_name = project_name
        self._flood = flood if flood is not None else FloodGate()
        self._now = now
        self._bot: object | None = None
        self._pending: list[str] = []
        self._refused_since: datetime | None = None
        self._sending = asyncio.Lock()

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(self._pending)

    def attach(self, bot: object) -> None:
        self._bot = bot

    async def notify(self, record: SessionRecord) -> None:
        self._pending.append(
            escape(unbound_notice(record, self._project_name(str(record.project_id))))
        )
        await self.pass_once()

    async def pass_once(self) -> None:
        """Send what is outstanding, stopping at a flood hold or a refusal."""
        if self._bot is None:
            return
        async with self._sending:
            while self._pending:
                if self._flood.held():
                    return
                try:
                    await self._view.send_apart(  # type: ignore[attr-defined]
                        self._bot, {"text": self._pending[0], "parse_mode": ParseMode.HTML}
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
