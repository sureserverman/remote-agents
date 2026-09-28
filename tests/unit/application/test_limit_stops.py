"""Which window stopped a session: one rule, asked by every place that records a limit stop.

A table rather than a narrative, because the rule is a precedence -- a live reading that shows a
saturated window beats the provider's own sentence, which beats nothing -- and every row below
is one place that precedence could be got backwards. `now` is an argument, so no clock is faked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from remote_agents.application.limit_stops import classify
from remote_agents.ports.agent_activity import LimitHit
from remote_agents.ports.agent_usage import AgentLimits, LimitsAbsence, UsageWindow

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
