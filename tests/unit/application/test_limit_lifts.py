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
from remote_agents.ports.agent_usage import AgentLimits, LimitsAbsence, UsagePart, UsageWindow

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


# --- Cursor's month: a live reading, one window in two pools --------------------------------

_CYCLE = _STOP + timedelta(days=4)
_NEXT_CYCLE = _CYCLE + timedelta(days=30)


def _cursor(
    total: float,
    cursor: float,
    other: float,
    *,
    at: datetime,
    resets_at: datetime = _CYCLE,
    live: bool = True,
) -> AgentLimits:
    month = UsageWindow(
        "month", total, resets_at, (UsagePart("cursor", cursor), UsagePart("other", other))
    )
    return AgentLimits("cursor-agent", (month,), observed_at=at, live=live)


_AFTER_CYCLE = _CYCLE + timedelta(minutes=1)

_CURSOR_CASES = [
    # --- at the cycle end, which only the reading could supply ---------------------------------
    pytest.param(
        LimitHit("month", _CYCLE),
        _cursor(1, 1, 0, at=_AFTER_CYCLE, resets_at=_NEXT_CYCLE),
        _CYCLE + timedelta(minutes=2),
        True,
        id="lifts-at-the-cycle-end",
    ),
    pytest.param(
        LimitHit("month", _CYCLE), None, _CYCLE + timedelta(minutes=2), True, id="lifts-unread"
    ),
    pytest.param(
        LimitHit("month", _CYCLE),
        _cursor(40, 100, 0, at=_AFTER_CYCLE, resets_at=_NEXT_CYCLE),
        _CYCLE + timedelta(minutes=2),
        False,
        id="a-pool-already-full-in-the-new-cycle-holds-it",
    ),
    pytest.param(
        LimitHit("month", _CYCLE),
        None,
        _CYCLE - timedelta(days=1),
        False,
        id="waits-for-the-cycle-end-unread",
    ),
    # --- early: the figures dropped inside the stop's own cycle ---------------------------------
    pytest.param(
        LimitHit("month", _CYCLE),
        _cursor(60, 60, 20, at=_EARLY),
        _EARLY_NOW,
        True,
        id="an-early-drop-lifts",
    ),
    pytest.param(
        LimitHit("month", _CYCLE),
        _cursor(70, 100, 20, at=_EARLY),
        _EARLY_NOW,
        False,
        id="an-early-drop-with-its-own-pool-still-full",
    ),
    pytest.param(
        LimitHit("month", _CYCLE),
        _cursor(70, 20, 99.6, at=_EARLY),
        _EARLY_NOW,
        False,
        id="an-early-drop-with-the-other-pool-still-full",
    ),
    pytest.param(
        LimitHit("month", _CYCLE),
        _cursor(100, 60, 20, at=_EARLY),
        _EARLY_NOW,
        False,
        id="an-early-drop-in-both-pools-with-the-total-still-full",
    ),
    pytest.param(
        LimitHit("month", _CYCLE),
        _cursor(60, 99.5, 20, at=_EARLY),
        _EARLY_NOW,
        False,
        id="a-pool-at-99.5-is-still-full",
    ),
    pytest.param(
        LimitHit("month", _CYCLE),
        _cursor(60, 60, 20, at=_EARLY, resets_at=_CYCLE + timedelta(minutes=30)),
        _EARLY_NOW,
        True,
        id="an-early-drop-dated-half-an-hour-off-is-the-same-cycle",
    ),
    pytest.param(
        LimitHit("month", _CYCLE),
        _cursor(60, 60, 20, at=_EARLY, resets_at=_CYCLE - timedelta(minutes=61)),
        _EARLY_NOW,
        False,
        id="an-early-drop-dated-61-minutes-earlier-is-not",
    ),
    pytest.param(
        LimitHit("month", _CYCLE),
        _cursor(60, 60, 20, at=_EARLY, live=False),
        _EARLY_NOW,
        False,
        id="a-drop-in-a-recording-is-not-evidence",
    ),
    pytest.param(
        LimitHit("month", _CYCLE),
        _cursor(60, 60, 20, at=_STOP + timedelta(seconds=50)),
        _EARLY_NOW,
        False,
        id="a-drop-read-inside-the-memo-minute",
    ),
    pytest.param(
        LimitHit("month", _CYCLE),
        _cursor(60, 60, 20, at=_EARLY, resets_at=_CYCLE - timedelta(days=2)),
        _EARLY_NOW,
        False,
        id="a-drop-dated-to-another-cycle-end",
    ),
    pytest.param(
        LimitHit("month", None),
        _cursor(60, 60, 20, at=_EARLY),
        _EARLY_NOW,
        False,
        id="a-stop-only-the-screen-named-never-lifts-from-a-reading",
    ),
    pytest.param(
        LimitHit(None, _CYCLE),
        _cursor(60, 60, 20, at=_EARLY),
        _EARLY_NOW,
        False,
        id="a-stop-with-no-window-never-lifts-from-a-reading",
    ),
    # --- early: a new cycle began ahead of the stop's cycle end -----------------------------------
    pytest.param(
        LimitHit("month", _CYCLE),
        _cursor(2, 2, 0, at=_EARLY, resets_at=_NEXT_CYCLE),
        _EARLY_NOW,
        True,
        id="a-new-cycle-lifts",
    ),
    pytest.param(
        LimitHit("month", _CYCLE),
        _cursor(2, 99.6, 0, at=_EARLY, resets_at=_NEXT_CYCLE),
        _EARLY_NOW,
        False,
        id="a-new-cycle-with-a-full-pool-does-not",
    ),
]


@pytest.mark.parametrize(("hit", "reading", "now", "expected"), _CURSOR_CASES)
def test_lifted_a_cursor_stop(
    hit: LimitHit, reading: AgentLimits | None, now: datetime, expected: bool
) -> None:
    assert lifted(hit, _STOP, reading, now=now) is expected


def test_a_live_drop_does_not_lift_a_cursor_stop_while_the_reading_is_stale() -> None:
    reading = _cursor(60, 60, 20, at=_EARLY)

    assert (
        lifted(LimitHit("month", _CYCLE), _STOP, reading, now=_EARLY + timedelta(hours=1)) is False
    )


# --- the early-lift margin: a live reading lifts early only below 95% (DEC-118) -------------


def _claude(percent: float, *, at: datetime, live: bool = True) -> AgentLimits:
    """Claude's two windows, the five-hour one in the stop's own period."""
    windows = (UsageWindow("5h", percent, _RESET), UsageWindow("week", 40, _WEEK))
    return AgentLimits("claude", windows, observed_at=at, live=live)


@pytest.mark.parametrize(
    ("hit", "reading", "expected"),
    [
        pytest.param(
            LimitHit("month", _CYCLE), _cursor(96, 60, 20, at=_EARLY), False, id="cursor-at-96"
        ),
        pytest.param(
            LimitHit("month", _CYCLE), _cursor(94, 60, 20, at=_EARLY), True, id="cursor-at-94"
        ),
        pytest.param(
            LimitHit("month", _CYCLE),
            _cursor(60, 96, 20, at=_EARLY),
            False,
            id="cursor-with-its-own-pool-at-96",
        ),
        pytest.param(
            LimitHit("month", _CYCLE),
            _cursor(94.9, 94.9, 94.9, at=_EARLY),
            True,
            id="cursor-everything-just-below-95",
        ),
        pytest.param(
            LimitHit("month", _CYCLE), _cursor(95, 60, 20, at=_EARLY), False, id="cursor-at-95"
        ),
        pytest.param(LimitHit("5h", _RESET), _claude(94, at=_EARLY), True, id="claude-live-at-94"),
        pytest.param(LimitHit("5h", _RESET), _claude(96, at=_EARLY), False, id="claude-live-at-96"),
        pytest.param(
            LimitHit("5h", _RESET),
            _claude(10, at=_EARLY, live=False),
            False,
            id="claude-from-the-status-line-never-lifts-early",
        ),
    ],
)
def test_a_live_reading_lifts_early_only_below_the_margin(
    hit: LimitHit, reading: AgentLimits, expected: bool
) -> None:
    assert lifted(hit, _STOP, reading, now=_EARLY_NOW) is expected
