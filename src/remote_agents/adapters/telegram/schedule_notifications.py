"""The one message a schedule's fire sends: what happened, and why in words (DEC-010).

`Scheduled: claude in remote-agents started` -- and, where it did not, the reason: skipped for a
limit or a run still working, missed while the service was down, started with its prompt not
typed. One message per fire (DEC-031 as amended); whether a fire is told at all is
`notification_policy.schedule_told`, decided once.

**Escaped here, once** (DEC-014): the project's name and the agent's id reach a surface that
parses HTML, and the schedule's own fields are stored raw.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from html import escape

from telegram.constants import ParseMode

from remote_agents.adapters.telegram.flood import FloodGate
from remote_agents.application.notification_policy import schedule_told
from remote_agents.application.relative_time import span
from remote_agents.application.schedules import STARTUP_PATIENCE, FireOutcome, FireReport

_LOG = logging.getLogger(__name__)

#: Consecutive refusals before a message is given up on, as the other notifiers count them.
_REFUSALS_BEFORE_ABANDONING = 3

_NOT_TYPED = {
    "dialog": "a dialog is open on the new session",
    "composing": "its composer already held text",
    "unconfirmed": "it may have been typed and was not seen to land; check it before sending again",
    "not_ready": f"the agent was not ready within {int(STARTUP_PATIENCE.total_seconds())} seconds",
    "shell": "it would run as a shell command",
    "no_composer": "this agent's composer cannot be read",
    "empty": "it is empty",
}
_NOT_STARTED = {
    "not_ready": "the agent did not become ready",
    "launch_error": "the launch failed",
}


def schedule_message(report: FireReport, *, project_name: str) -> str:
    """The sentence for one fire, or nothing for a fire the policy does not tell."""
    if not schedule_told(report.outcome):
        return ""
    agent = escape(str(report.schedule.profile_id))
    head = f"Scheduled: {agent} in {escape(project_name)}"
    outcome = report.outcome
    if outcome is FireOutcome.STARTED:
        return f"{head} started"
    if outcome is FireOutcome.NOT_TYPED:
        why = _NOT_TYPED.get(report.reason or "", "it could not be typed")
        return f"{head} started, but its prompt was not typed — {why}; the session is open"
    if outcome is FireOutcome.LAUNCH_FAILED:
        return (
            f"{head} did not start — {_NOT_STARTED.get(report.reason or '', 'the launch failed')}"
        )
    if outcome is FireOutcome.MISSED:
        late = span(int(report.late_by.total_seconds())) if report.late_by else "too"
        return f"{head} missed — the service was down at its time ({late} late)"
    if outcome is FireOutcome.SKIPPED_LIMIT:
        return f"{head} skipped — {agent} is at its usage limit"
    if outcome is FireOutcome.SKIPPED_PREVIOUS:
        return f"{head} skipped — its previous run is still working"
    raise AssertionError(f"no wording for {outcome!r}")


class ScheduleNotifier:
    """Send each fire's message apart from the live view, keeping one a refusal held back.

    The limit-reset notifier's shape: a message that could not be sent is held and tried again
    on the next pass, and given up after three consecutive refusals with one journal line. A
    fire is never detected twice, so dropping a refused message would lose it for good.
    """

    def __init__(
        self,
        *,
        view: object,
        project_name: Callable[[str], str],
        flood: FloodGate | None = None,
    ) -> None:
        self._view = view
        self._project_name = project_name
        self._flood = flood if flood is not None else FloodGate()
        self._bot: object | None = None
        #: Messages not yet delivered, oldest first, each with its consecutive refusals.
        self._pending: list[list[object]] = []

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(str(text) for text, _ in self._pending)

    def attach(self, bot: object) -> None:
        self._bot = bot

    async def notify(self, report: FireReport) -> None:
        """Queue the fire's message and send what is outstanding. Never raises."""
        text = schedule_message(
            report, project_name=self._project_name(str(report.schedule.project_id))
        )
        if text:
            self._pending.append([text, 0])
        await self.pass_once()

    async def pass_once(self) -> None:
        """Send what is outstanding, oldest first, stopping at a flood hold or a refusal."""
        if self._bot is None:
            return
        while self._pending:
            if self._flood.held():
                return
            entry = self._pending[0]
            try:
                await self._view.send_apart(  # type: ignore[attr-defined]
                    self._bot, {"text": entry[0], "parse_mode": ParseMode.HTML}
                )
            except Exception:
                entry[1] = int(entry[1]) + 1  # type: ignore[call-overload]
                if int(entry[1]) >= _REFUSALS_BEFORE_ABANDONING:  # type: ignore[call-overload]
                    _LOG.warning("a schedule notice was refused %d times; it is dropped", entry[1])
                    self._pending.pop(0)
                return
            self._pending.pop(0)
