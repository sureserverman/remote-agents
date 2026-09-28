"""Which usage window stopped a session, decided once for every place that records a stop.

A limit stop reaches this project three ways -- Claude's `StopFailure` hook, and the limit screen
Codex and Cursor Agent draw in their panes -- and each arrives knowing less than the owner wants
told: the hook says "rate limit", the screen says "you've hit your usage limit", and only
sometimes does the provider's own sentence name the window. So the window is decided here, from
two pieces of evidence the caller hands over, and never re-decided by a notifier or a feed row
(DEC-043).

**The measured figure beats the sentence.** A live reading showing a window at 100% is the
provider's own accounting at the moment of the stop; a hint parsed out of a sentence is the
provider's wording, which has changed before (Claude says "session limit" for its 5-hour window).
When they disagree the reading names the window, and the hint only fills an instant the reading
did not publish for that same window.

**When two windows are full, the later reset is what keeps the agent stopped.** A week window at
100% with a 5-hour window also at 100% lifts only when the week does, and the reset instant is
what the lift and the "carry on" after it wait for.

**Unknown is an answer, not a guess.** A stale reading or an absence falls through to the hint,
and with no hint either the answer is `LimitHit(None, None)`: the stop is still a stop, and
every surface keeps its window-blind wording for it. A full window with no published instant is
treated as the last to lift, because resuming on the other window's reset would type into an
agent that is still stopped. The same direction `limit_resets` fails in, for the same reason --
a wrong "weekly limit" would send the owner to wait days for something that lifts in hours.

**An instant without a zone is not evidence.** It can be neither compared with `now` nor stored
without guessing the host's offset, so a reading stamped with one is not read, a window carrying
one is skipped, and a hint's naive instant is dropped while its window is kept. That is what makes
the function total: nothing here can raise on a reading it did not expect.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime

from remote_agents.application.session_views import _STALE_READING_AGE
from remote_agents.domain.models import SessionId, SessionState
from remote_agents.ports.agent_activity import (
    ActivityConfidence,
    ActivityKind,
    AgentActivity,
    LimitHit,
    bounded_detail_line,
    reported_activity_kinds_for,
)
from remote_agents.ports.agent_usage import AgentLimits, UsageWindow
from remote_agents.ports.limit_screen import LimitScreen
from remote_agents.ports.session_store import SessionStore

_LOG = logging.getLogger(__name__)

#: The percentage at which a window counts as the one that stopped the agent: anything that rounds
#: to 100. Readers pass the provider's figure through unrounded, and nothing guarantees a blocked
#: window reads exactly 100, so a window the surfaces would draw as "100%" is a full one here too.
_SATURATED = 99.5


def classify(
    reading: AgentLimits | None,
    hint: LimitHit | None,
    *,
    now: datetime,
) -> LimitHit:
    """The window that stopped a session, from its provider's reading and its own sentence.

    Total: no input raises. `reading` is the provider's current account reading (or `None` when
    none was available); `hint` is what the provider's vertical parsed out of its own limit text.
    """
    saturated = _saturated(reading, now=now)
    if hint is not None and not _aware(hint.resets_at):
        hint = LimitHit(hint.window, None)
    if saturated is not None:
        resets_at = saturated.resets_at
        if resets_at is None and hint is not None and hint.window == saturated.label:
            resets_at = hint.resets_at
        return LimitHit(saturated.label, resets_at)
    if hint is not None:
        return hint
    return LimitHit(None, None)


def _saturated(reading: AgentLimits | None, *, now: datetime) -> UsageWindow | None:
    """The full window that lifts last, or `None` when the reading cannot name one.

    Staleness is `session_views`' own rule, asked rather than restated (DEC-043): a reading the
    surfaces would date is a reading this rule will not trust to name a window. A full window
    whose own reset has already passed is not what stops the agent now, so it names nothing.
    """
    if reading is None or reading.absence is not None:
        return None
    if reading.observed_at is not None:
        if not _aware(reading.observed_at) or now - reading.observed_at > _STALE_READING_AGE:
            return None
    full = [
        window
        for window in reading.windows
        if window.used_percent >= _SATURATED
        and _aware(window.resets_at)
        and (window.resets_at is None or window.resets_at > now)
    ]
    if not full:
        return None
    return max(full, key=_lifts_last)


def _lifts_last(window: UsageWindow) -> tuple[int, datetime | None]:
    """Order full windows by when they lift; one with no published instant lifts last.

    Equal instants keep the reading's own order (`max` returns the first of equals).
    """
    if window.resets_at is None:
        return (1, None)
    return (0, window.resets_at)


def _aware(instant: datetime | None) -> bool:
    """Whether an instant is absent or carries a zone -- the two shapes this rule can use."""
    return instant is None or instant.tzinfo is not None


class LimitStopClassifier:
    """Give each limit stop in a pass the window that stopped it, before it is recorded.

    The activity pass hands over what it gathered; every `LIMIT_REACHED` that arrived without a
    hit is classified with its provider's current reading and its provider's own sentence, and
    everything else passes through untouched. Application-layer under DEC-001: the provider
    readings, the session store and each vertical's `LimitScreen` are handed in, so nothing here
    knows which agent spells which sentence.

    **A failure costs the window, never the stop.** An unreadable reading, a session the store
    cannot find, or a hint that raised each leaves the activity as it arrived -- still a limit
    stop, still recorded and delivered, with the window-blind wording every surface already has.
    The pass that calls this runs beside the one that serves the owner and must not lose an
    observation to a classification it could do without.
    """

    def __init__(
        self,
        store: SessionStore,
        limits: Callable[[], Awaitable[Sequence[AgentLimits]]],
        screens: Mapping[str, LimitScreen],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._limits = limits
        self._screens = screens
        self._now = now

    async def classified(self, activities: Sequence[AgentActivity]) -> list[AgentActivity]:
        """The same activities in the same order, each unclassified limit stop classified."""
        if not any(_unclassified(activity) for activity in activities):
            return list(activities)
        now = self._now()
        readings = await self._readings()
        result: list[AgentActivity] = []
        for activity in activities:
            if _unclassified(activity):
                activity = await self._classify(activity, readings, now=now)
            result.append(activity)
        return result

    async def _classify(
        self, activity: AgentActivity, readings: Mapping[str, AgentLimits], *, now: datetime
    ) -> AgentActivity:
        try:
            record = await self._store.get(SessionId.parse(activity.session_id))
        except Exception:
            _LOG.warning("could not look up the session a limit stop belongs to")
            return activity
        if record is None:
            return activity
        profile = str(record.profile_id)
        screen = self._screens.get(profile)
        hint = None
        if screen is not None and activity.detail:
            try:
                hint = screen.hint(activity.detail, now)
            except Exception:
                _LOG.warning("a provider's limit sentence could not be read")
        return replace(activity, limit=classify(readings.get(profile), hint, now=now))

    async def _readings(self) -> dict[str, AgentLimits]:
        try:
            return {str(reading.profile_id): reading for reading in await self._limits()}
        except Exception:
            _LOG.warning("the limits read failed; limit stops this pass go by their own words")
            return {}


def _unclassified(activity: AgentActivity) -> bool:
    return activity.kind is ActivityKind.LIMIT_REACHED and activity.limit is None


#: How much of the bottom of a pane counts as "what the agent said last". Codex prints its limit
#: sentence just above its composer and Cursor draws it just below its status line, so both land
#: within the last dozen written lines; an answer that merely quotes the sentence scrolls it
#: above this window as soon as the answer is longer than a few lines.
_LAST_OUTPUT_LINES = 12

#: How many wrapped lines may continue a limit sentence. Codex's longest variant is about 150
#: characters, so three continuation lines cover it in a pane down to about 40 columns.
_CONTINUATION_LINES = 3

#: Bounds one pass: past this, the remaining sessions wait for the next pass rather than delay
#: the classification and delivery that run after this watch in the same activity pass.
_PASS_BUDGET_SECONDS = 15.0

#: Bounds one pane capture, for `CodexApprovalWatcher`'s reason: tmux's runner has no timeout of
#: its own, and a wedged server would otherwise stop this watch for the life of the process.
_CAPTURE_TIMEOUT_SECONDS = 5.0


class LimitScreenWatcher:
    """Find the limit stop an agent draws on its pane when it reports no limit event of its own.

    Codex fires no hook on a failed turn and Cursor Agent has no hooks at all, so the only
    place either says a usage limit stopped it is its screen. This watches exactly the running
    sessions whose provider declares a `limit_screen` and does **not** report `LIMIT_REACHED`
    itself -- Claude's `StopFailure` is the better evidence for Claude, and a second, inferred
    copy of it would be the redundancy DEC-066 retired `quiet` for.

    **What is read and what is kept.** Each pass captures the visible pane, matches the
    provider's markers against its last few written lines, and keeps one boolean per session:
    whether the marker was there. The capture itself is discarded. The emitted activity carries
    the matched line -- the agent's own words, bounded like any hook detail (DEC-037) -- because
    that line is what names the retry instant, and it is classified by the same pass that
    classifies every other stop.

    **Edge-triggered, seeded on first sight.** A session's first successful capture only records
    what it shows, so a restart over a limit screen that was already reported does not report it
    again; after that, a marker appearing is one stop, a marker still showing is nothing, and a
    marker gone re-arms. A capture that fails costs that session that pass and changes nothing.
    """

    def __init__(
        self,
        store: SessionStore,
        capture: Callable[[SessionId], Awaitable[str]],
        screens: Mapping[str, LimitScreen],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._capture = capture
        self._watched = {
            profile: screen
            for profile, screen in screens.items()
            if ActivityKind.LIMIT_REACHED not in reported_activity_kinds_for(profile)
        }
        self._now = now
        self._showing: dict[str, bool] = {}

    async def poll(self) -> tuple[AgentActivity, ...]:
        """Take one look at every running session whose stop only its screen can show."""
        records = await self._store.list((SessionState.RUNNING,))
        watched = [record for record in records if str(record.profile_id) in self._watched]
        live = {str(record.session_id) for record in watched}
        self._showing = {key: value for key, value in self._showing.items() if key in live}

        stops: list[AgentActivity] = []
        deadline = time.monotonic() + _PASS_BUDGET_SECONDS
        for record in watched:
            if time.monotonic() > deadline:
                _LOG.warning("the limit-screen watch ran out of time; the rest wait a pass")
                break
            key = str(record.session_id)
            try:
                screen = await asyncio.wait_for(
                    self._capture(record.session_id), timeout=_CAPTURE_TIMEOUT_SECONDS
                )
            except Exception:
                _LOG.warning("could not capture a pane while watching for a limit stop")
                continue
            line = _limit_line(screen, self._watched[str(record.profile_id)])
            seen_before = key in self._showing
            was_showing = self._showing.get(key, False)
            self._showing[key] = line is not None
            if line is None or was_showing or not seen_before:
                continue
            stops.append(
                AgentActivity(
                    session_id=key,
                    kind=ActivityKind.LIMIT_REACHED,
                    detail=bounded_detail_line(line),
                    observed_at=self._now(),
                    confidence=ActivityConfidence.INFERRED,
                )
            )
        return tuple(stops)


def _limit_line(screen: str, limit_screen: LimitScreen) -> str | None:
    """The sentence among the agent's last output that says it was stopped, or `None`.

    Returned with the lines that continue it, joined: a long sentence wraps in an ordinary pane
    (`capture-pane` keeps the wrap), and the part that names the retry instant is the part that
    lands on the next line. A continuation is an indented line; the first line that is not one
    ends the sentence.
    """
    tail = [line for line in screen.splitlines() if line.strip()][-_LAST_OUTPUT_LINES:]
    for index in range(len(tail) - 1, -1, -1):
        if any(re.search(marker, tail[index]) for marker in limit_screen.markers):
            sentence = [tail[index].strip()]
            for continuation in tail[index + 1 : index + 1 + _CONTINUATION_LINES]:
                if not continuation.startswith("  ") or _starts_a_line(continuation):
                    break
                sentence.append(continuation.strip())
            return " ".join(sentence)
    return None


def _starts_a_line(line: str) -> bool:
    """Whether an indented line opens something of its own (a bullet, a prompt, a box)."""
    return line.lstrip()[:1] in {"›", "■", "•", "→", "│", "╭", "╰", "─"}
