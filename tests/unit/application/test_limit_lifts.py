"""When a limit stop has lifted: one rule, failing toward "not yet".

Limit-lifecycle sub-plan 2 Task 2.1, corrected at its gate. A false lift would retire the owner's
limit line early and, once Stage 3 lands, type "carry on" into an agent that is still stopped, so
every uncertain case here answers `False`. A reading lifts a stop early only on *positive*
evidence that its window started a new period: Claude's status-line reading is stamped when the
line is drawn, not when its figures were measured, so "a reading after the stop showing the
window below full" can be the figures from before the stop, redrawn.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from remote_agents.application.limit_lifts import lifted
from remote_agents.ports.agent_activity import LimitHit
from remote_agents.ports.agent_usage import AgentLimits, LimitsAbsence, UsageWindow

_STOP = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
_RESET = _STOP + timedelta(hours=2)
_NEXT = _RESET + timedelta(hours=5)
"""The reset of the window's *next* period: what a reading shows once the window rolled over."""
_WEEK = _STOP + timedelta(days=3)


def _reading(*windows: UsageWindow, at: datetime) -> AgentLimits:
    return AgentLimits("codex", windows, observed_at=at)


def _window(label: str, percent: float, resets_at: datetime | None = _NEXT) -> UsageWindow:
    return UsageWindow(label, percent, resets_at)


_EARLY = _STOP + timedelta(minutes=10)
_EARLY_NOW = _STOP + timedelta(minutes=11)

_CASES = [
    # --- on schedule ---------------------------------------------------------------------
    pytest.param(
        LimitHit("5h", _RESET),
        None,
        _RESET + timedelta(seconds=61),
        True,
        id="scheduled-lift-a-minute-after-the-reset",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        None,
        _RESET + timedelta(seconds=59),
        False,
        id="scheduled-lift-waits-out-the-grace-minute",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("5h", 100, _NEXT), at=_RESET + timedelta(minutes=1)),
        _RESET + timedelta(minutes=2),
        False,
        id="still-saturated-after-resets-at",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("5h", 10), _window("week", 100, _WEEK), at=_RESET + timedelta(minutes=1)),
        _RESET + timedelta(minutes=2),
        False,
        id="another-window-still-full-holds-the-scheduled-lift",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("5h", 100, _RESET), at=_RESET + timedelta(minutes=1)),
        _RESET + timedelta(minutes=2),
        True,
        id="a-cached-full-window-whose-own-reset-lapsed-does-not-hold",
    ),
    pytest.param(
        LimitHit("opus week", _RESET),
        _reading(_window("week", 10), at=_STOP + timedelta(minutes=5)),
        _STOP + timedelta(minutes=6),
        False,
        id="a-model-week-lifts-on-its-reset-only",
    ),
    pytest.param(
        LimitHit("opus week", _RESET),
        None,
        _RESET + timedelta(minutes=2),
        True,
        id="a-model-week-lifts-on-schedule",
    ),
    pytest.param(
        LimitHit(None, _RESET),
        None,
        _RESET + timedelta(minutes=2),
        True,
        id="unknown-window-lifts-on-a-published-schedule",
    ),
    # --- early, from a reading ------------------------------------------------------------
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("5h", 12), at=_EARLY),
        _EARLY_NOW,
        True,
        id="early-the-window-started-a-new-period",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("5h", 97, _RESET), at=_EARLY),
        _EARLY_NOW,
        False,
        id="a-cached-pre-stop-reading-redrawn-after-the-stop",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("5h", 3, _RESET), at=_EARLY),
        _EARLY_NOW,
        False,
        id="a-wipe-that-kept-its-reset-waits-for-the-schedule",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("5h", 99.6), at=_EARLY),
        _EARLY_NOW,
        False,
        id="a-new-period-that-rounds-to-100-is-still-full",
    ),
    pytest.param(
        LimitHit("week", _WEEK),
        _reading(_window("week", 97, _WEEK + timedelta(minutes=40)), at=_EARLY),
        _EARLY_NOW,
        False,
        id="an-hour-level-screen-reset-is-not-a-new-period-40-minutes-later",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("5h", 12), _window("week", 100, _WEEK), at=_EARLY),
        _EARLY_NOW,
        False,
        id="early-held-by-another-full-window",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("5h", 12), at=_STOP + timedelta(seconds=50)),
        _EARLY_NOW,
        False,
        id="a-reading-inside-the-readers-memo-minute",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("5h", 12), at=_STOP - timedelta(minutes=1)),
        _EARLY_NOW,
        False,
        id="a-reading-older-than-the-stop",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("week", 12), at=_EARLY),
        _EARLY_NOW,
        False,
        id="a-different-window",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("5h", 12), at=_EARLY),
        _STOP + timedelta(minutes=45),
        False,
        id="a-stale-reading",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        AgentLimits("codex", (), observed_at=_EARLY, absence=LimitsAbsence.NO_READING),
        _EARLY_NOW,
        False,
        id="an-absence",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        AgentLimits("codex", (_window("5h", 12),)),
        _EARLY_NOW,
        False,
        id="a-reading-with-no-stamp-cannot-be-after-the-stop",
    ),
    pytest.param(
        LimitHit("month", None),
        _reading(_window("month", 5), at=_EARLY),
        _EARLY_NOW,
        False,
        id="a-named-window-with-no-published-reset-has-nothing-to-roll-past",
    ),
    # --- unknown window --------------------------------------------------------------------
    pytest.param(
        LimitHit(None, None),
        _reading(_window("5h", 30), _window("week", 60), at=_EARLY),
        _EARLY_NOW,
        False,
        id="unknown-window-never-lifts-from-a-reading",
    ),
    pytest.param(
        LimitHit(None, None),
        None,
        _STOP + timedelta(days=40),
        False,
        id="unknown-window-and-no-schedule-never-lifts",
    ),
    # --- instants that cannot be compared --------------------------------------------------
    pytest.param(
        LimitHit("5h", _RESET.replace(tzinfo=None)),
        None,
        _RESET + timedelta(hours=1),
        False,
        id="a-naive-reset-is-not-evidence",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("5h", 12), at=_EARLY.replace(tzinfo=None)),
        _EARLY_NOW,
        False,
        id="a-naive-reading-stamp-is-not-evidence",
    ),
]


@pytest.mark.parametrize(("hit", "reading", "now", "expected"), _CASES)
def test_lifted(hit: LimitHit, reading: AgentLimits | None, now: datetime, expected: bool) -> None:
    assert lifted(hit, _STOP, reading, now=now) is expected


def test_lifted_a_naive_stop_instant_is_never_after_anything() -> None:
    """The early witness compares the reading with the stop; a naive stop cannot be compared."""
    reading = _reading(_window("5h", 12), at=_EARLY)
    naive_stop = _STOP.replace(tzinfo=None)

    assert lifted(LimitHit("5h", _RESET), naive_stop, reading, now=_EARLY_NOW) is False
