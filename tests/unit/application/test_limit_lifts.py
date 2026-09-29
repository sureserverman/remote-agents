"""When a limit stop has lifted: one rule, failing toward "not yet".

Limit-lifecycle sub-plan 2 Task 2.1. A false lift would retire the owner's limit line early and,
once Stage 3 lands, type "carry on" into an agent that is still stopped, so every uncertain case
here answers `False`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from remote_agents.application.limit_lifts import lifted
from remote_agents.ports.agent_activity import LimitHit
from remote_agents.ports.agent_usage import AgentLimits, LimitsAbsence, UsageWindow

_STOP = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
_RESET = _STOP + timedelta(hours=2)


def _reading(*windows: UsageWindow, at: datetime) -> AgentLimits:
    return AgentLimits("codex", windows, observed_at=at)


def _window(label: str, percent: float, resets_at: datetime | None = _RESET) -> UsageWindow:
    return UsageWindow(label, percent, resets_at)


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
        _reading(_window("5h", 100, _RESET + timedelta(hours=5)), at=_RESET + timedelta(minutes=1)),
        _RESET + timedelta(minutes=2),
        False,
        id="still-saturated-after-resets-at",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("5h", 100), at=_STOP + timedelta(minutes=5)),
        _RESET + timedelta(minutes=2),
        True,
        id="a-full-reading-from-before-the-reset-does-not-hold-a-scheduled-lift",
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
    # --- early, from a reading ------------------------------------------------------------
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("5h", 12), at=_STOP + timedelta(minutes=10)),
        _STOP + timedelta(minutes=11),
        True,
        id="early-reset-seen-in-a-reading-after-the-stop",
    ),
    pytest.param(
        LimitHit("5h", None),
        _reading(_window("5h", 99.4), at=_STOP + timedelta(minutes=10)),
        _STOP + timedelta(minutes=11),
        True,
        id="early-just-under-the-saturation-line",
    ),
    pytest.param(
        LimitHit("5h", None),
        _reading(_window("5h", 99.6), at=_STOP + timedelta(minutes=10)),
        _STOP + timedelta(minutes=11),
        False,
        id="a-window-that-rounds-to-100-is-still-full",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("5h", 12), at=_STOP - timedelta(minutes=1)),
        _STOP + timedelta(minutes=11),
        False,
        id="a-reading-older-than-the-stop",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("week", 12), at=_STOP + timedelta(minutes=10)),
        _STOP + timedelta(minutes=11),
        False,
        id="a-different-window",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        _reading(_window("5h", 12), at=_STOP + timedelta(minutes=10)),
        _STOP + timedelta(minutes=45),
        False,
        id="a-stale-reading",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        AgentLimits(
            "codex", (), observed_at=_STOP + timedelta(minutes=5), absence=LimitsAbsence.NO_READING
        ),
        _STOP + timedelta(minutes=6),
        False,
        id="an-absence",
    ),
    pytest.param(
        LimitHit("5h", _RESET),
        AgentLimits("codex", (_window("5h", 12),)),
        _STOP + timedelta(minutes=6),
        False,
        id="a-reading-with-no-stamp-cannot-be-after-the-stop",
    ),
    # --- unknown window --------------------------------------------------------------------
    pytest.param(
        LimitHit(None, None),
        _reading(_window("5h", 30), _window("week", 60), at=_STOP + timedelta(minutes=10)),
        _STOP + timedelta(minutes=11),
        True,
        id="unknown-window-lifts-when-every-window-is-under",
    ),
    pytest.param(
        LimitHit(None, None),
        _reading(_window("5h", 30), _window("week", 100), at=_STOP + timedelta(minutes=10)),
        _STOP + timedelta(minutes=11),
        False,
        id="unknown-window-held-by-any-full-window",
    ),
    pytest.param(
        LimitHit(None, None),
        _reading(at=_STOP + timedelta(minutes=10)),
        _STOP + timedelta(minutes=11),
        False,
        id="unknown-window-and-a-reading-with-no-windows",
    ),
    pytest.param(
        LimitHit(None, None),
        None,
        _STOP + timedelta(days=40),
        False,
        id="unknown-window-and-no-reading-never-lifts",
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
        LimitHit("5h", None),
        _reading(_window("5h", 12), at=(_STOP + timedelta(minutes=10)).replace(tzinfo=None)),
        _STOP + timedelta(minutes=11),
        False,
        id="a-naive-reading-stamp-is-not-evidence",
    ),
]


@pytest.mark.parametrize(("hit", "reading", "now", "expected"), _CASES)
def test_lifted(hit: LimitHit, reading: AgentLimits | None, now: datetime, expected: bool) -> None:
    assert lifted(hit, _STOP, reading, now=now) is expected


def test_lifted_a_naive_stop_instant_is_never_after_anything() -> None:
    """The early witness compares the reading with the stop; a naive stop cannot be compared."""
    reading = _reading(_window("5h", 12), at=_STOP + timedelta(minutes=10))
    naive_stop = _STOP.replace(tzinfo=None)

    assert (
        lifted(LimitHit("5h", None), naive_stop, reading, now=_STOP + timedelta(minutes=11))
        is False
    )
