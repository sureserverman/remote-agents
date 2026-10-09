"""Whether a limit stop has lifted, decided once for the bot's line and the nudge after it.

A lift has three witnesses. **The schedule:** the provider published when the window resets, and
that instant, plus a minute of grace, has passed. **A new period:** a reading taken well after the
stop shows the stop's own window rolled over -- its reset now later than the one the stop was
recorded against -- and below full, which is how a window reopened ahead of its schedule is seen.
**A live drop:** a reading the provider was asked for well after the stop shows the stop's own
window clearly below full (`EARLY_LIFT_BELOW`) inside the stop's own period. Only a live reading
is a witness to that.

**It fails toward "not yet".** A false lift retires the owner's limit line while the agent is
still stopped and, with the resume switch on, types the nudge into it; a late lift costs a few
minutes of a line the owner can already see. So each witness is admitted only on positive
evidence, and the gate review of 2026-09-29 is why "positive" is the word:

- **A reading's stamp is not proof its figures are new.** Claude's status-line recording is dated
  when Code last *drew* the line, and it carries the rate limits the session cached from its last
  response -- so a redraw after the stop is a reading "after the stop" holding the figures from
  before it. In such a reading a drop below full proves nothing on its own; a window whose reset
  moved past the stop's does, because a cached figure carries the old reset.
- **Any full window whose own reset is still ahead holds the lift**, not only the stop's: after the
  five-hour window reopens, a full week still stops the agent. A full window whose reset has
  lapsed is a cached figure and holds nothing -- otherwise a status line that is never redrawn
  with new figures would hold the stop for ever.
- **A reading must be taken more than a minute after the stop**, past the readers' memo.
- **A stop whose window is unknown never lifts from a reading**, only on a published schedule:
  "nothing is full" is exactly the condition that left it unnamed. Nor does a named window with no
  published reset (Cursor's month as its screen names it), which has no period to roll past.
- **A live reading is the one exception to the first rule** (`AgentLimits.live`, DEC-118, which
  supersedes DEC-110 in part). Cursor's month and Claude's usage-API windows are asked of the
  provider's server when they are read, so their figures are as new as their stamp, and a stop's
  own window seen *clearly* below full in the stop's own period has reopened: below
  `EARLY_LIFT_BELOW`, the window and every pool in it, not merely below full. Only for a stop
  that carries that period's end, which it has from a reading that showed the window full: a
  stop the screen alone named was never measured full, so "below full" says nothing new about
  it.
- A Claude model week (`opus week`, ...) is a label no reading publishes, so it lifts on its own
  reset only.

**Accepted cost:** a provider that wipes a window in place, keeping its published reset, is not
lifted early -- the stop waits for its schedule. That wipe is exactly what the early-reset
notification (`limit_resets`, DEC-097) already tells the owner about, so they are not left
guessing; they are only not resumed automatically ahead of time.

Staleness is `session_views`' rule and "full" is `limit_stops.is_full`, both asked rather
than restated (DEC-043), so the window that named a stop and the window that lifts it are measured
by the same figure.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

from remote_agents.application.limit_resume import (
    LOOP_GUARD,
    LimitResume,
    NotResumed,
    Nudge,
    not_resumed,
)
from remote_agents.application.limit_stops import is_full
from remote_agents.application.session_views import _STALE_READING_AGE
from remote_agents.domain.models import SessionId, SessionState
from remote_agents.ports.agent_activity import LimitHit
from remote_agents.ports.agent_usage import AgentLimits, UsageWindow
from remote_agents.ports.limit_stop_outcomes import LIFTED, LimitStop, LimitStopOutcomes
from remote_agents.ports.session_store import SessionStore

_LOG = logging.getLogger(__name__)

NEW_PERIOD_MARGIN = timedelta(hours=1)
"""How far past the stop's reset a reading's reset must lie to count as a new period.

A stop's reset is often read off the agent's own screen rather than a reading, and Claude
prints a reset more than a day away at the hour (`resets Sep 30, 9am`,
`tests/unit/adapters/agents/claude/test_limit_hint.py`), so the stop's instant can sit up to an
hour before the provider's true one. A cached reading carrying that true reset would then look
like a new period. An hour covers the coarsest text measured, and costs nothing real: a window
that genuinely rolled over resets a whole window length later, and every window here is an hour
or longer. A wipe whose new reset lands within the hour waits for the schedule instead.
"""

EARLY_LIFT_BELOW = 95.0
"""How far a live reading's window must fall, in percent, before it lifts a stop early (DEC-118).

A separate threshold from `limit_stops.is_full` on purpose: the stop classifier shares that one,
and a window at 97% has not reopened in any sense the owner would act on. A false early lift
types "carry on" into an agent that is still stopped (DEC-109), so the window and each of its
pools must sit clearly below full. The schedule and a new period are untouched by it.
"""

READ_TIMEOUT_SECONDS = 8.0
"""How long one pass waits for the providers' readings before lifting on schedules alone."""

LIFT_GRACE = timedelta(seconds=60)
"""How long past a published reset the schedule alone waits before it calls the stop lifted.

Providers roll their counters over on their own clock, and this host's clock is not theirs; a
minute is the margin that keeps the nudge from landing a few seconds before the window opens.
"""


def lifted(
    hit: LimitHit,
    stopped_at: datetime,
    reading: AgentLimits | None,
    *,
    now: datetime,
) -> bool:
    """Whether the stop recorded as `hit` at `stopped_at` has lifted by `now`. Total and pure."""
    fresh = _fresh(reading, now=now)
    held = fresh is not None and _any_binding(fresh.windows, now=now)
    resets_at = hit.resets_at if _zoned(hit.resets_at) else None
    if resets_at is not None and now >= resets_at + LIFT_GRACE:
        return not held
    if held or fresh is None or hit.window is None or resets_at is None:
        return False
    if not _zoned(stopped_at) or fresh.observed_at <= stopped_at + LIFT_GRACE:
        return False
    own = [window for window in fresh.windows if window.label == hit.window]
    if not own:
        return False
    if all(_new_period(window, after=resets_at) for window in own):
        return True
    return fresh.live and all(_reopened(window, period_end=resets_at) for window in own)


def scheduled(hit: LimitHit, *, now: datetime) -> bool:
    """Whether the stop's own published reset, plus the grace minute, has passed."""
    return _zoned(hit.resets_at) and hit.resets_at is not None and now >= hit.resets_at + LIFT_GRACE


def _fresh(reading: AgentLimits | None, *, now: datetime) -> AgentLimits | None:
    """The reading, when it can be evidence at all: present, stamped with a zone, not stale."""
    if reading is None or reading.absence is not None or reading.observed_at is None:
        return None
    if not _zoned(reading.observed_at) or now - reading.observed_at > _STALE_READING_AGE:
        return None
    return reading


def _any_binding(windows: tuple[UsageWindow, ...], *, now: datetime) -> bool:
    """Whether any window is full and still in force: its reset ahead, or never published."""
    return any(
        is_full(window)
        and (window.resets_at is None or (_zoned(window.resets_at) and window.resets_at > now))
        for window in windows
    )


def _new_period(window: UsageWindow, *, after: datetime) -> bool:
    """Whether `window` is below full in a period that began after the stop's reset.

    "Began after" by `NEW_PERIOD_MARGIN`, never by the grace minute: see there.
    """
    return (
        not is_full(window)
        and _zoned(window.resets_at)
        and window.resets_at is not None
        and window.resets_at > after + NEW_PERIOD_MARGIN
    )


def _reopened(window: UsageWindow, *, period_end: datetime) -> bool:
    """Whether `window` is clearly below full in the period ending at `period_end`, the stop's own.

    "Clearly" is `EARLY_LIFT_BELOW`, for the window and every pool in it. The same period by
    `NEW_PERIOD_MARGIN`, the line `_new_period` draws from the other side. Evidence only from a
    live reading; the caller asks that.
    """
    return (
        window.used_percent < EARLY_LIFT_BELOW
        and all(part.used_percent < EARLY_LIFT_BELOW for part in window.parts)
        and _zoned(window.resets_at)
        and window.resets_at is not None
        and abs(window.resets_at - period_end) <= NEW_PERIOD_MARGIN
    )


def _zoned(instant: datetime | None) -> bool:
    """Whether an instant is present and carries a zone -- the only shape compared here."""
    return instant is not None and instant.tzinfo is not None


class LimitLiftWatcher:
    """Find each running session's undecided limit stop, and act once on the ones that lifted.

    Application-layer under DEC-001: the session store, the outcome store, the provider readings
    and the surface's retire call are handed in. A pass asks `lifted` of every running session
    whose newest news is still a limit stop; for each that lifted it retires the stop's line and
    then records `lifted`, so the stop is never acted on again -- not on the next pass, and not
    after a restart.

    **With a resume step, a lifted stop may be nudged first** (`limit_resume`): when the switch
    is on and the stop belongs to the session's current life. Then the order changes on
    purpose. The nudge goes only once the stop's line is settled -- sent, and the chat not held
    -- and its outcome is recorded *before* the line is touched, because a nudge typed twice is
    worse than a line left standing. A `NUDGING` intent is written just before the typing and
    replaced by the outcome; an intent a later process finds is given up as unconfirmed, never
    typed again. The switch is read again, the intent written, the text typed, the outcome
    recorded and the line updated as one step shielded from the pass's bound. A line that does
    not land is retried each pass until it does (in memory: a restart leaves it as it was).

    **A profile in `retire_only` is never nudged**, whatever the switch says: its agent keeps
    the owner's draft at the stop (`LimitScreen.keeps_draft`), so its lift takes the step below.

    **Without a nudge: retire, then record -- and only when the surface says the stop is done
    with.** Retiring a
    line that is already gone does nothing, so a crash between the two costs one repeat of a
    harmless step on restart. The other order would lose the retirement outright: recorded as
    done, never done, and never tried again. A retire that raises, or answers that the stop is not
    done yet (its notification still queued), records nothing, so the next pass tries again.
    """

    def __init__(
        self,
        store: SessionStore,
        outcomes: LimitStopOutcomes,
        limits: Callable[[], Awaitable[Sequence[AgentLimits]]],
        retire: Callable[[LimitStop], Awaitable[bool]],
        *,
        resume: LimitResume | None = None,
        retire_only: frozenset[str] = frozenset(),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._retire_only = retire_only
        self._store = store
        self._outcomes = outcomes
        self._limits = limits
        self._retire = retire
        self._resume = resume
        self._now = now
        #: Stops whose nudge step is running, possibly past a cancelled pass.
        self._in_flight: set[tuple[str, str]] = set()
        #: Lines a recorded nudge could not bring into line yet, retried each pass. In memory:
        #: a restart loses them, and the line stays as it was.
        self._owed: dict[tuple[str, str], tuple[LimitStop, NotResumed | None]] = {}

    async def pass_once(self) -> int:
        """How many stops this pass found lifted and acted on."""
        acted = await self._settle_interrupted()
        await self._retry_owed_lines()
        running = await self._store.list((SessionState.RUNNING,))
        profiles = {str(record.session_id): str(record.profile_id) for record in running}
        if not profiles:
            return acted
        stops = await self._outcomes.unresolved(tuple(profiles))
        if not stops:
            return acted
        readings = await self._readings()
        now = self._now()
        for stop in stops:
            if _key(stop) in self._in_flight:
                continue
            reading = readings.get(profiles[stop.session_id])
            if not lifted(stop.hit, stop.stopped_at, reading, now=now):
                continue
            nudged = profiles[stop.session_id] not in self._retire_only
            if nudged and await self._nudges(stop):
                acted += await self._nudge(stop, now=now)
            elif await self._retire_and_record(stop, now=now):
                acted += 1
        return acted

    async def _retire_and_record(self, stop: LimitStop, *, now: datetime) -> bool:
        """Stage 2's step: retire the line, and record `LIFTED` once the surface is done."""
        try:
            done = await self._retire(stop)
        except Exception:
            _LOG.warning("could not retire a lifted limit stop's line; it is tried again")
            return False
        if not done:
            # The surface is not finished with it yet -- its notification is still queued.
            return False
        try:
            await self._outcomes.record(stop, LIFTED, decided_at=now)
        except Exception:
            # Retiring again next pass is a no-op, so an unrecorded lift costs nothing.
            _LOG.warning("could not record a lifted stop; it is recorded next pass")
            return False
        return True

    async def _nudges(self, stop: LimitStop) -> bool:
        """Whether this stop is one to nudge: the switch is on, and it is this life's stop.

        "This life's": no lifecycle event since the stop. A running session writes none -- the
        state machine has no running-to-running step -- so an event after the stop means the
        session went through a stop request, a trust question or a restart since, and the old
        stop is not what it is doing now.
        """
        if self._resume is None or not await self._resume.enabled():
            return False
        try:
            events = await self._store.events(SessionId.parse(stop.session_id))
        except Exception:
            _LOG.warning("could not read a session's history; its lifted stop is not nudged")
            return False
        return all(event.created_at <= stop.stopped_at for event in events)

    async def _nudge(self, stop: LimitStop, *, now: datetime) -> int:
        """Nudge one stop, record what came of it, then retire or amend its line."""
        resume = self._resume
        assert resume is not None
        try:
            last = await self._outcomes.last_resumed_at(stop.session_id)
        except Exception:
            _LOG.warning("could not read when a session was last nudged; it is asked next pass")
            return 0
        if last is not None and last <= stop.stopped_at < last + LOOP_GUARD:
            # Stopped again straight after a nudge: only its own published reset lifts it, and
            # only one still ahead when the nudge went -- a reset already past is the false
            # lift repeating.
            reset = stop.hit.resets_at
            if not (scheduled(stop.hit, now=now) and reset is not None and reset > last):
                return 0
        try:
            if not await resume.settled(stop):
                return 0
        except Exception:
            _LOG.warning("could not ask whether a stop's line is settled; it is tried again")
            return 0
        # One shielded step from the switch's last read to the line: the service bounds each
        # pass, and a bound firing between the typing and the record -- or between the record
        # and the line -- would leave the nudge unrecorded or the line stale.
        key = _key(stop)
        self._in_flight.add(key)
        task = asyncio.ensure_future(self._act(stop, now=now))
        task.add_done_callback(lambda _done: self._in_flight.discard(key))
        return await asyncio.shield(task)

    async def _act(self, stop: LimitStop, *, now: datetime) -> int:
        """Read the switch again, write the intent, type, and finish -- or back out untyped."""
        resume = self._resume
        assert resume is not None
        if not resume.holds(stop):
            if not await resume.enabled():
                return 0
            try:
                claimed = await self._outcomes.claim(stop, decided_at=now)
            except Exception:
                _LOG.warning("could not write a nudge's intent; nothing is typed this pass")
                return 0
            if not claimed:
                # Decided already -- by a step that finished after this pass read the stop.
                return 0
        verdict = await resume.nudge(stop)
        if verdict.outcome is None or verdict.outcome == LIFTED:
            # Nothing was typed: take the intent back.
            resume.forget(stop)
            await self._release(stop)
            if verdict.outcome is None:
                return 0
            return int(await self._retire_and_record(stop, now=now))
        return await self._finish(stop, verdict, now=now)

    async def _finish(self, stop: LimitStop, verdict: Nudge, *, now: datetime) -> int:
        """Record the verdict over the intent, then bring the line into line with it."""
        resume = self._resume
        assert resume is not None and verdict.outcome is not None
        try:
            recorded = await self._outcomes.record(stop, verdict.outcome, decided_at=now)
        except Exception:
            # The verdict stays held and the intent stays written, so the next pass records it
            # (`_settle_interrupted`) without typing again.
            _LOG.exception("a nudge's outcome could not be recorded; it is recorded next pass")
            return 0
        resume.forget(stop)
        if not recorded:
            # The stop was already final -- the settle loop read it before its own step
            # finished -- and that step has the line; saying anything here would contradict it.
            return 0
        if not await self._update_line(stop, verdict.reason):
            self._owed[_key(stop)] = (stop, verdict.reason)
        return 1

    async def _settle_interrupted(self) -> int:
        """Finish every intent this process is not already acting on.

        One this process holds a verdict for is a record that failed: it is recorded now. One
        it holds nothing for was written by a process that stopped between the typing and the
        record, so whether the text landed is unknown -- it is given up as unconfirmed, and
        never typed again.
        """
        if self._resume is None:
            return 0
        try:
            interrupted = await self._outcomes.interrupted()
        except Exception:
            _LOG.warning("could not read the nudges left unfinished; they are read next pass")
            return 0
        now = self._now()
        settled = 0
        for stop in interrupted:
            if _key(stop) in self._in_flight:
                continue
            held = self._resume.held(stop)
            verdict = held or Nudge(not_resumed(NotResumed.UNCONFIRMED), NotResumed.UNCONFIRMED)
            settled += await self._finish(stop, verdict, now=now)
        return settled

    async def _retry_owed_lines(self) -> None:
        """Bring each line a recorded nudge left stale into line, until it lands."""
        for key, (stop, reason) in list(self._owed.items()):
            if await self._update_line(stop, reason):
                self._owed.pop(key, None)

    async def _update_line(self, stop: LimitStop, reason: NotResumed | None) -> bool:
        """Retire a resumed stop's line, or amend a not-resumed one's. Whether it landed."""
        resume = self._resume
        assert resume is not None
        try:
            if reason is None:
                done = await self._retire(stop)
            else:
                done = await resume.amend(stop, reason.value)
        except Exception:
            done = False
        if not done:
            _LOG.warning("a nudged stop's line could not be updated yet; it is tried again")
        return done

    async def _release(self, stop: LimitStop) -> None:
        try:
            await self._outcomes.release(stop)
        except Exception:
            # Left written, the intent reads as interrupted next pass and is given up as
            # unconfirmed: a lost nudge, never a doubled one.
            _LOG.warning("could not take back a nudge's intent")

    async def _readings(self) -> dict[str, AgentLimits]:
        """Each provider's reading, or none: a schedule needs no reading to lift a stop.

        Bounded on its own, inside the pass's bound, so a slow reader costs this pass its early
        witness and its holds, never the schedule lifts that need no reading at all.
        """
        try:
            readings = await asyncio.wait_for(self._limits(), timeout=READ_TIMEOUT_SECONDS)
            return {str(reading.profile_id): reading for reading in readings}
        except Exception:
            _LOG.warning("the limits read failed; this pass lifts stops on their schedule only")
            return {}


def _key(stop: LimitStop) -> tuple[str, str]:
    return (stop.session_id, stop.stamp)
