"""Which window stopped a session: one rule, asked by every place that records a limit stop.

A table rather than a narrative, because the rule is a precedence -- a live reading that shows a
saturated window beats the provider's own sentence, which beats nothing -- and every row below
is one place that precedence could be got backwards. `now` is an argument, so no clock is faked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from remote_agents.application.limit_stops import LimitStopClassifier, classify
from remote_agents.ports.agent_activity import (
    ActivityConfidence,
    ActivityKind,
    AgentActivity,
    LimitHit,
)
from remote_agents.ports.agent_usage import AgentLimits, LimitsAbsence, UsageWindow
from remote_agents.ports.limit_screen import LimitScreen

_NOW = datetime(2026, 9, 28, 21, 0, tzinfo=UTC)
_IN_2H = _NOW + timedelta(hours=2)
_IN_3D = _NOW + timedelta(days=3)


def _reading(*windows: UsageWindow, age: timedelta = timedelta(minutes=1)) -> AgentLimits:
    return AgentLimits("claude", windows, observed_at=_NOW - age, stale_source="status line")


_FIVE_HOUR_FULL = UsageWindow("5h", 100, _IN_2H)
_FIVE_HOUR_LOW = UsageWindow("5h", 12, _IN_2H)
_WEEK_FULL = UsageWindow("week", 100, _IN_3D)
_WEEK_LOW = UsageWindow("week", 80, _IN_3D)


@pytest.mark.parametrize(
    ("reading", "hint", "expected"),
    [
        pytest.param(
            _reading(_FIVE_HOUR_FULL, _WEEK_LOW), None, LimitHit("5h", _IN_2H), id="5h-saturated"
        ),
        pytest.param(
            _reading(_FIVE_HOUR_LOW, _WEEK_FULL),
            None,
            LimitHit("week", _IN_3D),
            id="week-saturated",
        ),
        # Both full: the one that lifts last is the one that keeps the agent stopped.
        pytest.param(
            _reading(_FIVE_HOUR_FULL, _WEEK_FULL),
            None,
            LimitHit("week", _IN_3D),
            id="both-saturated-latest-reset-wins",
        ),
        pytest.param(
            _reading(_FIVE_HOUR_LOW, _WEEK_LOW),
            LimitHit("5h", _IN_2H),
            LimitHit("5h", _IN_2H),
            id="hint-only",
        ),
        # A reading and the provider's sentence disagree: the measured figure names the window.
        pytest.param(
            _reading(_FIVE_HOUR_LOW, _WEEK_FULL),
            LimitHit("5h", _IN_2H),
            LimitHit("week", _IN_3D),
            id="reading-beats-hint",
        ),
        # A saturated window with no published instant borrows the hint's, when they agree.
        pytest.param(
            _reading(UsageWindow("5h", 100)),
            LimitHit("5h", _IN_2H),
            LimitHit("5h", _IN_2H),
            id="hint-fills-a-missing-instant",
        ),
        pytest.param(
            _reading(_FIVE_HOUR_FULL, age=timedelta(hours=1)),
            LimitHit("week", _IN_3D),
            LimitHit("week", _IN_3D),
            id="stale-reading-ignored",
        ),
        pytest.param(
            AgentLimits("claude", observed_at=_NOW, absence=LimitsAbsence.NO_READING),
            LimitHit("5h", None),
            LimitHit("5h", None),
            id="absence-falls-to-hint",
        ),
        pytest.param(None, None, LimitHit(None, None), id="nothing-known"),
        # A full window whose own reset has already passed is not what is stopping the agent
        # now -- a reading up to 30 minutes old can still show one.
        pytest.param(
            _reading(UsageWindow("5h", 100, _NOW - timedelta(minutes=10)), _WEEK_LOW),
            None,
            LimitHit(None, None),
            id="an-expired-full-window-names-nothing",
        ),
        # Two full windows, one with no published instant: unknown is the later lift, never a
        # guess that resumes the agent while the other window still blocks it.
        pytest.param(
            _reading(_FIVE_HOUR_FULL, UsageWindow("week", 100)),
            None,
            LimitHit("week", None),
            id="an-unknown-instant-lifts-last",
        ),
        # Providers need not round: a figure that rounds to 100 is a full window.
        pytest.param(
            _reading(UsageWindow("5h", 99.6, _IN_2H)),
            None,
            LimitHit("5h", _IN_2H),
            id="a-figure-that-rounds-to-100-is-full",
        ),
        pytest.param(
            _reading(UsageWindow("day", 100, _IN_2H)),
            None,
            LimitHit("day", _IN_2H),
            id="an-unfamiliar-label-is-passed-through",
        ),
    ],
)
def test_the_window_that_stopped_a_session(
    reading: AgentLimits | None, hint: LimitHit | None, expected: LimitHit
) -> None:
    assert classify(reading, hint, now=_NOW) == expected


def test_naive_instants_are_never_trusted_and_never_raise() -> None:
    """Totality: an instant without a zone cannot be compared or stored honestly, so it is
    treated as unknown rather than raising inside a service loop or being stored shifted by the
    host's offset."""
    naive_now = datetime(2026, 9, 28, 21, 0)
    naive_reading = AgentLimits(
        "claude", (UsageWindow("5h", 100, naive_now + timedelta(hours=2)),), observed_at=naive_now
    )
    mixed = _reading(UsageWindow("5h", 100, naive_now + timedelta(hours=2)), _WEEK_FULL)

    assert classify(naive_reading, None, now=_NOW) == LimitHit(None, None)
    assert classify(mixed, None, now=_NOW) == LimitHit("week", _IN_3D)
    assert classify(None, LimitHit("5h", naive_now), now=_NOW) == LimitHit("5h", None)


def test_a_model_week_the_reading_cannot_see_keeps_its_own_later_instant() -> None:
    """Claude's Opus week is in no reading, so a full 5h window cannot stand in for it."""
    opus = LimitHit("opus week", _IN_3D)
    assert classify(_reading(_FIVE_HOUR_FULL, _WEEK_LOW), opus, now=_NOW) == opus


def test_a_full_window_that_lifts_after_a_model_week_still_wins() -> None:
    opus = LimitHit("opus week", _IN_2H)
    week = _reading(_FIVE_HOUR_LOW, _WEEK_FULL)
    assert classify(week, opus, now=_NOW) == LimitHit("week", _IN_3D)


class _Record:
    def __init__(self, session_id, profile_id) -> None:
        self.session_id = session_id
        self.profile_id = profile_id


class _Store:
    async def get(self, session_id):
        return _Record(session_id, "codex")


def _stop(confidence):
    return AgentActivity(
        "11111111-1111-4111-8111-111111111111",
        ActivityKind.LIMIT_REACHED,
        "■ You’ve hit your usage limit.",
        _NOW,
        confidence,
    )


def _codex(week_percent: float) -> AgentLimits:
    return AgentLimits(
        "codex",
        (_FIVE_HOUR_LOW, UsageWindow("week", week_percent, _IN_3D)),
        observed_at=_NOW,
        stale_source="app server",
    )


async def test_an_inferred_stop_the_reading_cannot_yet_name_waits_for_a_fresher_reading() -> None:
    """The account reading is remembered for a minute, so the one read in the stop's own pass
    usually predates it; the stop is held a pass or two for the reading that names its window."""

    readings = [(_codex(97),), (_codex(100),)]

    async def limits():
        return readings.pop(0)

    screen = LimitScreen(markers=(".",), hint=lambda text, now: LimitHit(None, None))
    classifier = LimitStopClassifier(_Store(), limits, {"codex": screen}, now=lambda: _NOW)

    assert await classifier.classified([_stop(ActivityConfidence.INFERRED)]) == []
    (released,) = await classifier.classified([])
    assert released.limit == LimitHit("week", _IN_3D)


async def test_a_held_stop_is_released_window_blind_when_no_reading_names_it() -> None:

    async def limits():
        return (_codex(97),)

    screen = LimitScreen(markers=(".",), hint=lambda text, now: LimitHit(None, None))
    classifier = LimitStopClassifier(_Store(), limits, {"codex": screen}, now=lambda: _NOW)

    released = await classifier.classified([_stop(ActivityConfidence.INFERRED)])
    for _ in range(4):
        released += await classifier.classified([])
    (stop,) = released
    assert stop.limit == LimitHit(None, None)


async def test_a_reported_stop_is_never_held() -> None:
    """A stop drained from the spool is gone from disk; holding it in memory could lose it."""

    async def limits():
        return (_codex(97),)

    screen = LimitScreen(markers=(".",), hint=lambda text, now: LimitHit(None, None))
    classifier = LimitStopClassifier(_Store(), limits, {"codex": screen}, now=lambda: _NOW)

    (stop,) = await classifier.classified([_stop(ActivityConfidence.REPORTED)])
    assert stop.limit == LimitHit(None, None)
