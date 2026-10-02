"""The schedule record: a `when` is a one-shot local instant or a repeat on chosen weekdays."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from remote_agents.ports.schedules import Once, Repeat, Weekday


def test_a_repeat_with_no_days_is_refused() -> None:
    with pytest.raises(ValueError, match="day"):
        Repeat(frozenset(), time(9, 0))


def test_daily_carries_all_seven_days() -> None:
    assert Repeat.daily(time(9, 0)).days == frozenset(Weekday)
    assert len(Repeat.daily(time(9, 0)).days) == 7


def test_weekdays_carries_monday_to_friday() -> None:
    days = Repeat.weekdays(time(9, 0)).days
    assert len(days) == 5
    assert days == frozenset({Weekday.MON, Weekday.TUE, Weekday.WED, Weekday.THU, Weekday.FRI})


def test_a_once_must_be_naive_local_wall_time() -> None:
    Once(datetime(2026, 10, 2, 3, 0))
    with pytest.raises(ValueError, match="local"):
        Once(datetime(2026, 10, 2, 3, 0, tzinfo=UTC))


def test_a_repeat_time_must_be_naive_wall_time() -> None:
    with pytest.raises(ValueError, match="local"):
        Repeat.daily(time(9, 0, tzinfo=UTC))


def test_seconds_are_dropped_from_a_wall_time() -> None:
    assert Once(datetime(2026, 10, 2, 3, 0, 41, 7)).at == datetime(2026, 10, 2, 3, 0)
    assert Repeat.daily(time(9, 0, 12)).time == time(9, 0)
