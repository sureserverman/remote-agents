"""The one message a failed rollover sends: which session, why in words, and that it was kept.

`Rollover failed: claude in remote-agents #3 — the successor did not take over within 30 minutes;
predecessor preserved` -- naming a successor left open where the failure left one, and, for a
stop that did not go through, that the successor has taken over and the stop is the owner's to
finish (force stop stays theirs, DEC-007). "Preserved" is said only where it is known.

**At most once, never twice.** The pass tells at the terminal move, which the store makes once,
and the queue is in memory: a `serve` restart while a refused notice is held drops it, with no
journal line. The row's note ("rollover failed · predecessor preserved") is the standing
record. Only these two ends
are told (`notification_policy.rollover_told`, DEC-031): every other move, a completed rollover
above all, is the row's redraw (DEC-090).

The schedule notices' shape (`schedule_notifications.py`): a refused message is held and tried
again on the next tick, and given up with one journal line after `_GIVE_UP_AFTER`.

**A sent message deletes itself** once its rollover is resolved -- the session it names has
stopped, or a newer rollover of that session has opened -- or `NOTICE_LIFETIME` after it was
sent, whichever comes first. The row's note stays the standing record. **Escaped
here, once** (DEC-014): the project's name reaches a surface that parses HTML.
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
from remote_agents.application.notification_policy import rollover_told
from remote_agents.application.rollover import (
    ADOPTION_PATIENCE,
    NOT_IDLE_PATIENCE,
    REQUEST_PATIENCE,
    TRUST_PATIENCE,
    RolloverReport,
)
from remote_agents.domain.rollover import RolloverState
from remote_agents.ports.expiring_notices import ExpiringNoticeStore

_LOG = logging.getLogger(__name__)

#: How long a refused notice is tried again before it is given up, as the schedules' are.
_GIVE_UP_AFTER = timedelta(minutes=10)

#: How long a sent notice stays at most; the owner's choice, 2026-10-18.
NOTICE_LIFETIME = timedelta(hours=1)

#: The kind its sent messages are remembered under (`ports.expiring_notices`).
NOTICE_KIND = "rollover-failed"

_MINUTES_IDLE = int(NOT_IDLE_PATIENCE.total_seconds() // 60)
_MINUTES_ADOPT = int(ADOPTION_PATIENCE.total_seconds() // 60)
_HOURS_ASKED = int(REQUEST_PATIENCE.total_seconds() // 3600)
_HOURS_TRUST = int(TRUST_PATIENCE.total_seconds() // 3600)

#: Why a rollover failed, in words. A code missing here is shown as itself.
_WHY = {
    "predecessor-not-idle": f"the session was not idle for {_MINUTES_IDLE} minutes",
    "predecessor-gone": "the session stopped before its successor was up",
    "launch-failed": "the successor's launch failed, perhaps part-way; check its sessions",
    "successor-unknown": "a successor may have started unrecorded; check its sessions",
    "successor-untrusted": f"the successor was not trusted within {_HOURS_TRUST} hours",
    "successor-failed": "the successor failed or went away",
    "not-typed": "the handoff command was not typed into the successor",
    "adoption-timeout": f"the successor did not take over within {_MINUTES_ADOPT} minutes",
    "id-mismatch": "the successor was handed the wrong handoff",
    "no-ready": "the successor found no handoff to take over",
    "cwd-mismatch": "the successor started in another directory",
    "branch-mismatch": "the successor found the checkout on another branch",
    "plan-missing": "the successor could not find the plan",
    "not-rollable": "this agent cannot roll over",
    "no-handoff-root": "the project is not in a git checkout",
    "request-unwritten": "the request could not be handed to the workflow",
    "request-lost": "the request was gone before the workflow answered it",
    "request-unanswered": f"the workflow did not answer the request within {_HOURS_ASKED} hours",
}

#: Why the stop of a rollover's predecessor did not go through. No duration: the not-idle
#: refusal may be an instant one, judged under the stop's own key lock.
_WHY_STOP = {
    "predecessor-not-idle": "it was not idle when the stop was due",
    "not_idle": "it was not idle when the stop was due",
    "stop-unconfirmed": "the stop could not be confirmed; check the session",
    "graceful_timeout": "it did not exit in time",
    "composer_holds_text": "its input held text",
    "keys_busy": "something else was typing into it",
    "agent_asking": "it was asking a question",
    "unknown_session": "its pane could not be found",
}

#: FAILED codes that end with the predecessor no longer running: "preserved" would be false.
_PREDECESSOR_NOT_KEPT = frozenset({"predecessor-gone"})

#: FAILED codes that end with the launched successor still open, so the owner may want it gone.
_SUCCESSOR_LEFT_OPEN = frozenset(
    {
        "not-typed",
        "successor-untrusted",
        "adoption-timeout",
        "id-mismatch",
        "no-ready",
        "cwd-mismatch",
        "branch-mismatch",
        "plan-missing",
    }
)

#: STOP_FAILED codes where the stop may have landed: the predecessor's fate is not known.
_STOP_MAY_HAVE_LANDED = frozenset({"stop-unconfirmed", "unknown_session"})


def rollover_message(report: RolloverReport, *, project_name: str) -> str:
    """The sentence for one ended rollover, or nothing for an end the policy does not tell."""
    rollover = report.rollover
    if not rollover_told(rollover.state):
        return ""
    session = f"{escape(str(rollover.profile_id))} in {escape(project_name)}"
    if report.predecessor is not None:
        session = f"{session} #{report.predecessor.display.sequence}"
    code = rollover.failure_code or ""
    if rollover.state is RolloverState.STOP_FAILED:
        why = _WHY_STOP.get(code, escape(code) or "the stop was not confirmed")
        kept = "" if code in _STOP_MAY_HAVE_LANDED else "; predecessor preserved"
        return (
            f"Rollover stop failed: {session} — {why}{kept}. Its successor has taken over: "
            "stop this session when it is done, or force stop it"
        )
    why = _WHY.get(code, escape(code) or "it did not complete")
    if code == "not-typed" and rollover.failure_detail:
        why = f"{why} ({escape(rollover.failure_detail)})"
    text = f"Rollover failed: {session} — {why}"
    if code not in _PREDECESSOR_NOT_KEPT:
        text = f"{text}; predecessor preserved"
    if code in _SUCCESSOR_LEFT_OPEN:
        number = "" if report.successor is None else f" #{report.successor.display.sequence}"
        text = f"{text}. Its successor{number} is still open: stop it if it is not needed"
    return text


@dataclass
class _Held:
    text: str
    rollover_id: str
    refused_since: datetime | None = None


class RolloverNotifier:
    """Send each failed rollover's message apart from the live view, holding a refused one.

    `ScheduleNotifier`'s shape, and the same one-sender rule: two drains reading the same head
    would send one message twice and drop the next.
    """

    def __init__(
        self,
        *,
        view: object,
        project_name: Callable[[str], str],
        flood: FloodGate | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        notices: ExpiringNoticeStore | None = None,
        settled: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._view = view
        self._project_name = project_name
        self._messages = ExpiringMessages(view=view, kind=NOTICE_KIND, store=notices)
        #: Whether a rollover's notice is over before its lifetime, by rollover id
        #: (`application.rollover.rollover_notice_settled`). None removes by time alone.
        self._settled = settled
        self._flood = flood if flood is not None else FloodGate()
        self._now = now
        self._bot: object | None = None
        #: Messages not yet delivered, oldest first.
        self._pending: list[_Held] = []
        self._sending = asyncio.Lock()

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(held.text for held in self._pending)

    def attach(self, bot: object) -> None:
        self._bot = bot

    async def notify(self, report: RolloverReport) -> None:
        """Queue the rollover's message and send what is outstanding. Never raises."""
        text = rollover_message(
            report, project_name=self._project_name(str(report.rollover.project_id))
        )
        if text:
            self._pending.append(_Held(text, report.rollover.id))
        await self.pass_once()

    async def pass_once(self) -> None:
        """Send what is outstanding, oldest first, stopping at a flood hold or a refusal; then
        delete the sent notices that are resolved or due."""
        if self._bot is None:
            return
        async with self._sending:
            await self._send_pending()
            await self._messages.sweep(self._bot, self._now(), settled=self._settled)

    async def _send_pending(self) -> None:
        while self._pending:
            if self._flood.held():
                return
            held = self._pending[0]
            try:
                message_id = await self._view.send_apart(  # type: ignore[attr-defined]
                    self._bot, {"text": held.text, "parse_mode": ParseMode.HTML}
                )
            except Exception:
                now = self._now()
                held.refused_since = held.refused_since or now
                if now - held.refused_since >= _GIVE_UP_AFTER:
                    _LOG.warning("a rollover notice was refused for 10 minutes; dropped")
                    self._pending.remove(held)
                return
            self._pending.remove(held)
            await self._messages.sent(
                self._bot,
                message_id,
                subject=held.rollover_id,
                expires_at=self._now() + NOTICE_LIFETIME,
            )
