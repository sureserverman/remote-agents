"""Whether a limit stop has lifted, decided once for the bot's line and the "carry on" after it.

A lift has two witnesses. **The schedule:** the provider published when the window resets, and
that instant, plus a minute of grace, has passed. **A new period:** a reading taken well after the
stop shows the stop's own window rolled over -- its reset now later than the one the stop was
recorded against -- and below full, which is how a window reopened ahead of its schedule is seen.

**It fails toward "not yet".** A false lift retires the owner's limit line while the agent is
still stopped and, with the resume switch on, types "carry on" into it; a late lift costs a few
minutes of a line the owner can already see. So each witness is admitted only on positive
evidence, and the gate review of 2026-09-29 is why "positive" is the word:

- **A reading's stamp is not proof its figures are new.** Claude's status-line recording is dated
  when Code last *drew* the line, and it carries the rate limits the session cached from its last
  response -- so a redraw after the stop is a reading "after the stop" holding the figures from
  before it. A drop below full proves nothing on its own; a window whose reset moved past the
  stop's does, because a cached figure carries the old reset.
- **Any full window whose own reset is still ahead holds the lift**, not only the stop's: after the
  five-hour window reopens, a full week still stops the agent. A full window whose reset has
  lapsed is a cached figure and holds nothing -- otherwise a status line that is never redrawn
  with new figures would hold the stop for ever.
- **A reading must be taken more than a minute after the stop**, past the readers' memo.
- **A stop whose window is unknown never lifts from a reading**, only on a published schedule:
  "nothing is full" is exactly the condition that left it unnamed. Nor does a named window with no
  published reset (Cursor's month), which has no period to roll past.
- A Claude model week (`opus week`, ...) is a label no reading publishes, so it lifts on its own
  reset only.

**Accepted cost:** a provider that wipes a window in place, keeping its published reset, is not
lifted early -- the stop waits for its schedule. That wipe is exactly what the early-reset
notification (`limit_resets`, DEC-097) already tells the owner about, so they are not left
guessing; they are only not resumed automatically ahead of time.

Staleness is `session_views`' rule and "full" is `limit_stops`' saturation line, both asked rather
than restated (DEC-043), so the window that named a stop and the window that lifts it are measured
by the same figure.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

from remote_agents.application.limit_stops import _SATURATED
from remote_agents.application.session_views import _STALE_READING_AGE
from remote_agents.domain.models import SessionState
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

READ_TIMEOUT_SECONDS = 8.0
"""How long one pass waits for the providers' readings before lifting on schedules alone."""

LIFT_GRACE = timedelta(seconds=60)
"""How long past a published reset the schedule alone waits before it calls the stop lifted.

Providers roll their counters over on their own clock, and this host's clock is not theirs; a
minute is the margin that keeps "carry on" from landing a few seconds before the window opens.
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
    return bool(own) and all(_new_period(window, after=resets_at) for window in own)


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
        window.used_percent >= _SATURATED
        and (window.resets_at is None or (_zoned(window.resets_at) and window.resets_at > now))
        for window in windows
    )


def _new_period(window: UsageWindow, *, after: datetime) -> bool:
    """Whether `window` is below full in a period that began after the stop's reset.

    "Began after" by `NEW_PERIOD_MARGIN`, never by the grace minute: see there.
    """
    return (
        window.used_percent < _SATURATED
        and _zoned(window.resets_at)
        and window.resets_at is not None
        and window.resets_at > after + NEW_PERIOD_MARGIN
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

    **Retire, then record -- and only when the surface says the stop is done with.** Retiring a
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
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._outcomes = outcomes
        self._limits = limits
        self._retire = retire
        self._now = now

    async def pass_once(self) -> int:
        """How many stops this pass found lifted and acted on."""
        running = await self._store.list((SessionState.RUNNING,))
        profiles = {str(record.session_id): str(record.profile_id) for record in running}
        if not profiles:
            return 0
        stops = await self._outcomes.unresolved(tuple(profiles))
        if not stops:
            return 0
        readings = await self._readings()
        now = self._now()
        acted = 0
        for stop in stops:
            reading = readings.get(profiles[stop.session_id])
            if not lifted(stop.hit, stop.stopped_at, reading, now=now):
                continue
            try:
                done = await self._retire(stop)
            except Exception:
                _LOG.warning("could not retire a lifted limit stop's line; it is tried again")
                continue
            if not done:
                # The surface is not finished with it yet -- its notification is still queued.
                continue
            await self._outcomes.record(stop, LIFTED, decided_at=now)
            acted += 1
        return acted

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
