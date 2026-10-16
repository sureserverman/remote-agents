"""The one message a restart that lost the tmux server sends: what came back, and what did not.

`Resumed 2 sessions after restart`, then one line per session that was not resumed, or was
resumed without its nudge, saying why. Nothing at all when nothing was interrupted. This is
the notice DEC-127 amends DEC-031 for: one per restart, about sessions that are live again.

The rollover notices' shape (`rollover_notifications.py`): sent apart from the live view, at once
when the bot is attached, else held until it attaches -- the pass runs beside polling, so it can
finish either side of that -- and given up with one journal line after `_GIVE_UP_AFTER`.
**Escaped here, once** (DEC-014): the project's name reaches a surface that parses HTML.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from html import escape

from telegram.constants import ParseMode

from remote_agents.adapters.telegram.flood import FloodGate
from remote_agents.application.interrupted_resume import DEFERRED, NOT_NUDGED, ResumeReport
from remote_agents.ports.interrupted_resumes import RESUMED, SKIPPED

_LOG = logging.getLogger(__name__)

#: How long a refused notice is tried again before it is given up, as the rollovers' are.
_GIVE_UP_AFTER = timedelta(minutes=10)

#: Why a session was not resumed, in words: one entry per reason the pass can write
#: (`ResumeSkip` and the pass's own `skipped:` outcomes). A reason missing here is shown as
#: itself, so a test walks both sources against this table.
_WHY_SKIPPED = {
    "not-resumable": "this agent cannot resume a conversation",
    "handed-off": "it had handed its work to a successor",
    "not-adopted": "its rollover had not been accepted; the session it was taking over came back",
    "already-open": "its conversation is already open",
    "no-conversation": "no conversation was recorded for it",
    "superseded": "a newer session had already opened its conversation",
    "worktree_removed": "its worktree was removed",
    "interrupted": "an earlier start may have resumed it part-way; check its sessions",
    "launch_error": "the resume failed, perhaps part-way; check its sessions",
    "launch_failed": "the resumed session did not start",
}

#: Why a resumed session was not nudged, in words: one entry per value the nudge can end on --
#: every `PromptReason` a refused send names, every `DeliveryVerdict` it falls back to, and the
#: pass's own `not_ready` and `stopping`. A reason missing here is shown as itself, so a test
#: walks those sources against this table.
_WHY_NOT_NUDGED = {
    "dialog": "a dialog was showing",
    "menu": "a menu was showing",
    "not_ready": "the agent was not ready in time",
    "unconfirmed": "the message may not have landed",
    "submit_not_seen": "the message may not have been submitted",
    "draft_not_seen": "the message was not seen in its input",
    "timeout": "typing it took too long",
    "stopping": "the service was stopping",
    "composing": "its input already held text",
    "busy": "the agent was busy",
    "keys_busy": "someone else was typing into it",
    "not_running": "the resumed session was not running",
    "no_composer": "this agent has no input to type into",
    "unrecognised": "its screen was not recognised",
    "tmux_error": "tmux failed before anything was typed",
    "shell": "the message would have run as a shell command",
    "empty": "there was nothing to type",
    "refused": "the message was refused",
}


def restart_message(
    reports: tuple[ResumeReport, ...], *, unchecked: int, project_name: Callable[[str], str]
) -> str:
    """The notice for one restart, or nothing when there is nothing to tell."""

    def label(report: ResumeReport) -> str:
        record = report.record
        project = escape(project_name(str(record.project_id)))
        return f"{escape(str(record.profile_id))} in {project} #{record.display.sequence}"

    resumed = [report for report in reports if report.outcome.startswith(f"{RESUMED}:")]
    lines: list[str] = []
    if resumed:
        noun = "session" if len(resumed) == 1 else "sessions"
        lines.append(f"Resumed {len(resumed)} {noun} after restart")
    not_resumed = [
        f"• {label(report)} — {_skipped(report.outcome)}"
        for report in reports
        if not report.outcome.startswith(f"{RESUMED}:")
    ]
    if not_resumed:
        lines.extend(["Not resumed:", *not_resumed])
    not_nudged = [
        f"• {label(report)} — {_not_nudged(report.outcome)}"
        for report in resumed
        if f"; {NOT_NUDGED}:" in report.outcome
    ]
    if not_nudged:
        lines.extend(["Resumed, but not told to carry on:", *not_nudged])
    if unchecked:
        noun = "session" if unchecked == 1 else "sessions"
        lines.append(
            f"Could not tell whether {unchecked} {noun} lost their panes in a restart; "
            "none were resumed"
        )
    return "\n".join(lines)


def _skipped(outcome: str) -> str:
    if outcome == DEFERRED:
        return "it will be tried again at the next start"
    reason = outcome.removeprefix(f"{SKIPPED}:")
    return _WHY_SKIPPED.get(reason, escape(reason))


def _not_nudged(outcome: str) -> str:
    reason = outcome.rpartition(f"; {NOT_NUDGED}:")[2]
    return _WHY_NOT_NUDGED.get(reason, escape(reason))


class RestartNotifier:
    """Send the restart notice apart from the live view, holding it until it is delivered."""

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

    async def notify(self, reports: tuple[ResumeReport, ...], unchecked: int) -> None:
        """Queue the restart's notice and send what is outstanding. Never raises."""
        text = restart_message(reports, unchecked=unchecked, project_name=self._project_name)
        if text:
            self._pending.append(text)
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
                        _LOG.warning("the restart notice was refused for 10 minutes; dropped")
                        self._pending.pop(0)
                        self._refused_since = None
                    return
                self._pending.pop(0)
                self._refused_since = None
