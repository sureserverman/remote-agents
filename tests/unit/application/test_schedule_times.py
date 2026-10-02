"""The next fire of a schedule, in the host's local wall clock, across both DST transitions."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from remote_agents.application.schedule_times import host_zone, next_fire
from remote_agents.ports.schedules import Once, Repeat, Weekday

BERLIN = ZoneInfo("Europe/Berlin")


def _local(*parts: int, fold: int = 0) -> datetime:
    return datetime(*parts, tzinfo=BERLIN, fold=fold).astimezone(UTC)


def test_a_gap_time_fires_at_the_first_instant_after_the_gap() -> None:
    # 2026-03-29 is the spring-forward Sunday in Berlin: 02:00 jumps to 03:00.
    after = _local(2026, 3, 29, 1, 0)
    fired = next_fire(Repeat(frozenset({Weekday.SUN}), time(2, 30)), after, BERLIN)
    assert fired == _local(2026, 3, 29, 3, 0)
    assert fired.astimezone(BERLIN).hour == 3


def test_a_folded_time_fires_once_at_its_first_occurrence() -> None:
    # 2026-10-25 is the fall-back Sunday in Berlin: 02:00-03:00 happens twice.
    when = Repeat(frozenset({Weekday.SUN}), time(2, 30))
    first = next_fire(when, _local(2026, 10, 25, 1, 0), BERLIN)
    assert first == _local(2026, 10, 25, 2, 30, fold=0)
    # Asked again from the first firing, the second 02:30 is not another fire: the next one is
    # a week later.
    again = next_fire(when, first, BERLIN)
    assert again == _local(2026, 11, 1, 2, 30)


def test_daily_nine_stays_nine_local_across_both_transitions() -> None:
    when = Repeat.daily(time(9, 0))
    for day in (28, 29, 30):
        fired = next_fire(when, _local(2026, 3, day - 1, 9, 0), BERLIN)
        assert fired == _local(2026, 3, day, 9, 0)
    for day in (24, 25, 26):
        fired = next_fire(when, _local(2026, 10, day - 1, 9, 0), BERLIN)
        assert fired == _local(2026, 10, day, 9, 0)
    # One UTC hour apart across the change, which is the wall clock holding still.
    assert _local(2026, 3, 30, 9, 0).hour != _local(2026, 3, 28, 9, 0).hour


def test_weekdays_from_a_friday_morning_go_to_monday() -> None:
    # 2026-10-02 is a Friday.
    fired = next_fire(Repeat.weekdays(time(9, 0)), _local(2026, 10, 2, 10, 0), BERLIN)
    assert fired == _local(2026, 10, 5, 9, 0)


def test_a_repeat_fires_later_today_when_its_time_is_still_ahead() -> None:
    fired = next_fire(Repeat.daily(time(9, 0)), _local(2026, 10, 2, 8, 59), BERLIN)
    assert fired == _local(2026, 10, 2, 9, 0)


def test_a_repeat_whose_time_is_now_moves_to_its_next_day() -> None:
    fired = next_fire(Repeat.daily(time(9, 0)), _local(2026, 10, 2, 9, 0), BERLIN)
    assert fired == _local(2026, 10, 3, 9, 0)


def test_a_one_shot_in_the_future_fires_at_its_instant() -> None:
    fired = next_fire(Once(datetime(2026, 10, 3, 3, 0)), _local(2026, 10, 2, 12, 0), BERLIN)
    assert fired == _local(2026, 10, 3, 3, 0)


def test_a_one_shot_in_the_past_is_none() -> None:
    assert next_fire(Once(datetime(2026, 10, 1, 3, 0)), _local(2026, 10, 2, 12, 0), BERLIN) is None


def test_the_answer_is_utc() -> None:
    fired = next_fire(Repeat.daily(time(9, 0)), _local(2026, 10, 2, 8, 0), BERLIN)
    assert fired is not None
    assert fired.utcoffset() is not None
    assert fired.tzinfo is UTC


def test_host_zone_reads_the_localtime_symlink(tmp_path: Path) -> None:
    link = tmp_path / "localtime"
    link.symlink_to("/usr/share/zoneinfo/Europe/Berlin")
    assert host_zone(link) == BERLIN


def test_host_zone_without_a_symlink_is_utc(tmp_path: Path) -> None:
    assert host_zone(tmp_path / "localtime") == ZoneInfo("UTC")
    plain = tmp_path / "plain"
    plain.write_text("not a link")
    assert host_zone(plain) == ZoneInfo("UTC")


def test_host_zone_with_an_unknown_target_is_utc(tmp_path: Path) -> None:
    link = tmp_path / "localtime"
    link.symlink_to("/nowhere/zoneinfo/Not/AZone")
    assert host_zone(link) == ZoneInfo("UTC")


def test_the_doctor_names_the_zone_or_the_fallback(tmp_path: Path) -> None:
    from remote_agents.application.doctor import production_doctor
    from remote_agents.application.schedule_times import schedule_zone_line

    link = tmp_path / "localtime"
    link.symlink_to("/usr/share/zoneinfo/Europe/Berlin")
    assert schedule_zone_line(link) == "Europe/Berlin"
    assert schedule_zone_line(tmp_path / "absent") == (
        f"UTC (fallback: {tmp_path / 'absent'} names no zone)"
    )
    report = production_doctor(
        core_ready=True,
        database_ready=True,
        tmux_ready=True,
        telegram_ready=True,
        service_ready=True,
        profiles=(),
        registered_projects=0,
        discovered_projects=0,
        catalogue_projects=0,
        schedule_zone="Europe/Berlin",
    )
    assert report["schedule_zone"] == "Europe/Berlin"


def test_in_one_hour_is_an_hour_of_elapsed_time_across_both_transitions() -> None:
    from remote_agents.application.schedule_times import preset_time

    for before in (_local(2026, 3, 29, 1, 30), _local(2026, 10, 25, 1, 30)):
        at = preset_time("in_1h", before, BERLIN)
        fired = next_fire(Once(at), before, BERLIN)
        assert fired == before + timedelta(hours=1), (before, at, fired)


def test_a_typed_date_outside_the_next_few_years_is_not_a_time() -> None:
    from remote_agents.application.schedule_times import parse_time_text

    now = _local(2026, 10, 2, 12, 0)
    assert parse_time_text("2026-12-24 18:00", now, BERLIN) == datetime(2026, 12, 24, 18, 0)
    assert parse_time_text("2031-01-01 00:00", now, BERLIN) is not None
    for text in ("9999-12-31 23:59", "0001-01-01 00:00", "2032-01-01 00:00", "2024-12-31 23:59"):
        assert parse_time_text(text, now, BERLIN) is None, text


def test_only_ascii_digits_are_a_time() -> None:
    from remote_agents.application.schedule_times import parse_time_text

    now = _local(2026, 10, 2, 12, 0)
    assert parse_time_text("٠٩:٣٠", now, BERLIN) is None
    assert parse_time_text("24:00", now, BERLIN) is None
    assert parse_time_text("09:60", now, BERLIN) is None
    assert parse_time_text(" 9:05 ", now, BERLIN) == datetime(2026, 10, 3, 9, 5)


def test_a_one_shot_at_the_edge_of_the_calendar_never_fires_rather_than_raising() -> None:
    new_york = ZoneInfo("America/New_York")
    assert (
        next_fire(Once(datetime(9999, 12, 31, 23, 59)), _local(2026, 10, 2, 0, 0), new_york) is None
    )
    assert next_fire(Once(datetime(1, 1, 1, 0, 0)), _local(2026, 10, 2, 0, 0), BERLIN) is None
