"""Fire the schedules that are due: start a session, type the prompt, say what happened.

The service runs `SchedulePass.run` on a clock of its own. Each due schedule is checked in the
owner's order (rules 4-6), and either launched or passed over:

- **more than `MISSED_GRACE` late** -- the service was down at its time -- it is *missed*;
- **its agent has an active limit stop** it is *skipped*: a launch would only stop again;
- **the session its previous run started is still working** it is *skipped*;
- **otherwise** it is launched, under the key `sched:<id>:<fire instant>` (rule 8), so a restart
  part-way through a fire finds the key claimed and launches nothing a second time.

A launched session is then given its prompt through the terminal's guarded send (rules 2 and 7,
DEC-099): tried again while the new agent boots -- busy, not yet running, a screen not yet
recognised -- for `STARTUP_PATIENCE`; given up at once on a dialog, a draft already in the
composer, or a refusal of the text; and never tried again once it may have typed
(unconfirmed). A session that came up on its folder-trust dialog is not typed into at all. A
prompt that was not typed leaves the session open for the owner, and the notice says why. The
send's answer is read by `prompt_delivery`, the table the limit nudge reads too (DEC-043).

Every branch advances the schedule past this fire -- a recurring one to its next time from now,
never a backlog; a one-shot is done -- and every branch is reported once (DEC-031 as amended for
this kind). A check that raises leaves the schedule due, to be tried next pass; past the grace it
is reported missed, so a fault cannot fire it late or silently.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from enum import StrEnum

from remote_agents.application.commands import LaunchCommand
from remote_agents.application.errors import DuplicateCommandError
from remote_agents.application.prompt_delivery import DeliveryVerdict, delivery_verdict
from remote_agents.application.schedule_times import next_fire
from remote_agents.domain.models import ProfileId, SessionId, SessionState
from remote_agents.ports.schedules import Schedule, ScheduleStore
from remote_agents.ports.terminal import PromptDelivery

_LOG = logging.getLogger(__name__)

MISSED_GRACE = timedelta(minutes=15)
"""How late a fire may still start: a restart within this of its time fires it (rule 4)."""

STARTUP_PATIENCE = timedelta(seconds=90)
"""How long a new session's agent is given to show an idle composer before its prompt is dropped."""

RETRY_SECONDS = 3.0
"""How long a booting session is given between tries of its first prompt."""

#: What a booting agent shows on its way to an idle composer, so a first prompt waits it out.
_BOOTING = frozenset(
    {DeliveryVerdict.WAIT, DeliveryVerdict.NOT_RUNNING, DeliveryVerdict.UNRECOGNISED}
)

SCHEDULED_LABEL = "scheduled"
"""The label a scheduled session is launched with, so the owner can tell it from their own."""


class FireOutcome(StrEnum):
    """What one fire came to -- a key the surfaces word, never shown as itself (DEC-043)."""

    STARTED = "started"
    """Launched, and the prompt was typed."""
    NOT_TYPED = "not_typed"
    """Launched, and the prompt was not typed; the session is left open for the owner."""
    LAUNCH_FAILED = "launch_failed"
    MISSED = "missed"
    SKIPPED_LIMIT = "skipped_limit"
    SKIPPED_PREVIOUS = "skipped_previous"
    DUPLICATE = "duplicate"
    """The fire's key was already claimed: a restart found this fire already launched."""


@dataclass(frozen=True, slots=True)
class FireReport:
    """One fire, as the owner is told about it."""

    schedule: Schedule
    outcome: FireOutcome
    fired_at: datetime
    """The instant the schedule was due, in UTC."""
    session_id: str | None = None
    late_by: timedelta | None = None
    """Set on a missed fire: how far past its time the service came back."""
    reason: str | None = None
    """Why a prompt was not typed (a `PromptReason` key, or `dialog`), or a launch failed."""


class SchedulePass:
    """One pass over the due schedules. Everything outside the application is handed in."""

    def __init__(
        self,
        store: ScheduleStore,
        *,
        launch: Callable[[LaunchCommand], Awaitable[object]],
        send: Callable[[SessionId, str], Awaitable[PromptDelivery]],
        limit_stopped: Callable[[ProfileId], Awaitable[bool]],
        working: Callable[[str], Awaitable[bool]],
        notify: Callable[[FireReport], Awaitable[None]],
        zone: Callable[[], tzinfo],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._launch = launch
        self._send = send
        self._limit_stopped = limit_stopped
        self._working = working
        self._notify = notify
        self._zone = zone
        self._now = now
        self._sleep = sleep

    async def run(self) -> tuple[FireReport, ...]:
        """Fire everything due now, and return what each fire came to.

        Every launch is made first and recorded before any prompt is typed, so a crash while
        typing finds the fire recorded; the prompts are then typed side by side, so one slow
        agent does not hold another's prompt back.
        """
        now = self._now()
        fired: list[FireReport] = []
        for schedule in await self._store.due(now):
            try:
                fired.append(await self._fire(schedule, now))
            except Exception:
                _LOG.exception("schedule %s could not be fired; it stays due", schedule.id)
        reports = await asyncio.gather(*(self._delivered(report) for report in fired))
        for report in reports:
            try:
                await self._notify(report)
            except Exception:
                _LOG.exception(
                    "the notice for schedule %s could not be handed on", report.schedule.id
                )
        return tuple(reports)

    async def _delivered(self, report: FireReport) -> FireReport:
        """Type a started session's prompt, and say what that came to."""
        if report.outcome is not FireOutcome.STARTED or report.session_id is None:
            return report
        reason = await self._first_prompt(SessionId.parse(report.session_id), report.schedule)
        if reason is None:
            return report
        return replace(report, outcome=FireOutcome.NOT_TYPED, reason=reason)

    async def _first_prompt(self, session_id: SessionId, schedule: Schedule) -> str | None:
        """None once the prompt landed, else why it was not typed."""
        started = self._now()
        while True:
            try:
                delivery = await self._send(session_id, schedule.prompt)
            except Exception:
                # The send never raises by contract; a raise here may have typed, so it is
                # given up rather than tried again (DEC-099).
                _LOG.exception("typing schedule %s's prompt failed partway", schedule.id)
                return DeliveryVerdict.UNCONFIRMED.value
            verdict = delivery_verdict(delivery)
            if verdict is DeliveryVerdict.SENT:
                return None
            if verdict not in _BOOTING:
                if verdict is DeliveryVerdict.REFUSED and delivery.reason is not None:
                    return delivery.reason.value
                return verdict.value
            if self._now() - started >= STARTUP_PATIENCE:
                return "not_ready"
            await self._sleep(RETRY_SECONDS)

    async def _fire(self, schedule: Schedule, now: datetime) -> FireReport:
        assert schedule.next_fire_at is not None  # `due` returns only rows with a time
        due_at = schedule.next_fire_at
        late = now - due_at
        if late > MISSED_GRACE:
            return await self._record(
                schedule, now, FireReport(schedule, FireOutcome.MISSED, due_at, late_by=late)
            )
        if await self._limit_stopped(schedule.profile_id):
            return await self._record(
                schedule, now, FireReport(schedule, FireOutcome.SKIPPED_LIMIT, due_at)
            )
        previous = schedule.last_session_id
        if previous is not None and await self._working(previous):
            return await self._record(
                schedule, now, FireReport(schedule, FireOutcome.SKIPPED_PREVIOUS, due_at, previous)
            )
        return await self._launch_for(schedule, now, due_at)

    async def _launch_for(self, schedule: Schedule, now: datetime, due_at: datetime) -> FireReport:
        command = LaunchCommand(
            schedule.project_id,
            schedule.profile_id,
            f"sched:{schedule.id}:{due_at.astimezone(UTC).isoformat()}",
            SCHEDULED_LABEL,
        )
        try:
            outcome = await self._launch(command)
        except DuplicateCommandError:
            return await self._record(
                schedule, now, FireReport(schedule, FireOutcome.DUPLICATE, due_at)
            )
        except Exception:
            # The launch may have got part-way, so this fire is over either way: recorded and
            # reported, never retried (its key is claimed already).
            _LOG.exception("launching schedule %s failed", schedule.id)
            return await self._record(
                schedule,
                now,
                FireReport(schedule, FireOutcome.LAUNCH_FAILED, due_at, reason="launch_error"),
            )
        record = outcome.record  # type: ignore[attr-defined]
        session_id = str(record.session_id)
        if record.state is SessionState.FAILED:
            return await self._record(
                schedule,
                now,
                FireReport(
                    schedule, FireOutcome.LAUNCH_FAILED, due_at, session_id, reason="not_ready"
                ),
            )
        if record.state is SessionState.UNTRUSTED:
            # Up on its folder-trust dialog: typing would answer the dialog (rule 7).
            return await self._record(
                schedule,
                now,
                FireReport(schedule, FireOutcome.NOT_TYPED, due_at, session_id, reason="dialog"),
            )
        return await self._record(
            schedule, now, FireReport(schedule, FireOutcome.STARTED, due_at, session_id)
        )

    async def _record(self, schedule: Schedule, now: datetime, report: FireReport) -> FireReport:
        """Advance the schedule past this fire, from now: a missed or skipped run is not owed."""
        after = max(now, report.fired_at)
        following = next_fire(schedule.when, after, self._zone())
        await self._store.record_fire(schedule.id, now, report.session_id, following)
        return report
