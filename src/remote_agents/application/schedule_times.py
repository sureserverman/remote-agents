"""When a schedule fires next: a local wall-clock date and time, made an instant only at the end.

PEP 495 is why it is done this way round. An aware `datetime` in a `ZoneInfo` keeps whatever wall
time it is given, including one inside the spring-forward gap that never happens, and picks
between the two readings of a fall-back hour by `fold`. So the candidate is chosen as a local
date and time, and turned into an instant by a round trip through UTC, which moves a gap time
forward past the gap; a folded time keeps `fold=0`, its first occurrence. Every comparison is
made in UTC.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from remote_agents.ports.schedules import Once, Repeat, Weekday, When

LOCALTIME = Path("/etc/localtime")
"""The host's zone, as a symlink into the zoneinfo tree on both Linux and macOS."""

_ZONEINFO_MARKER = "zoneinfo/"


def next_fire(when: When, after: datetime, zone: tzinfo) -> datetime | None:
    """The first fire strictly after `after`, in UTC; None for a one-shot already past."""
    if isinstance(when, Once):
        fired = _instant(when.at.date(), when.at.time(), zone)
        return fired if fired > after else None
    return _next_repeat(when, after, zone)


def _next_repeat(when: Repeat, after: datetime, zone: tzinfo) -> datetime:
    start = after.astimezone(zone).date()
    # Eight days covers every weekday once from any start, today included.
    for offset in range(8):
        day = start + timedelta(days=offset)
        if day.weekday() not in when.days:
            continue
        fired = _instant(day, when.time, zone)
        if fired > after:
            return fired
    raise AssertionError("a repeat with at least one day fires within eight days")


def _instant(day: date, wall: time, zone: tzinfo) -> datetime:
    local = datetime(day.year, day.month, day.day, wall.hour, wall.minute, tzinfo=zone, fold=0)
    if _exists(local, zone):
        return local.astimezone(UTC)
    return _gap_end(local, zone)


def _exists(local: datetime, zone: tzinfo) -> bool:
    """Whether the wall time happens: a gap time comes back from UTC as another wall time."""
    return local.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == local.replace(tzinfo=None)


def _gap_end(local: datetime, zone: tzinfo) -> datetime:
    """The first instant after the gap `local` falls in -- where the wall clock jumped to.

    The two readings of a gap time, by the offsets before and after the jump, bracket the jump
    instant; it is found a minute at a time, which is at most the length of the gap.
    """
    early, late = sorted(
        (local.replace(fold=1).astimezone(UTC), local.replace(fold=0).astimezone(UTC))
    )
    offset = early.astimezone(zone).utcoffset()
    moment = early
    while moment < late:
        moment += timedelta(minutes=1)
        if moment.astimezone(zone).utcoffset() != offset:
            return moment
    return late


def host_zone(path: Path = LOCALTIME) -> ZoneInfo:
    """The host's zone from the `localtime` symlink's target, or UTC when it cannot be read."""
    zone = host_zone_name(path)
    return ZoneInfo(zone) if zone is not None else ZoneInfo("UTC")


def host_zone_name(path: Path = LOCALTIME) -> str | None:
    """The zone's key (`Europe/Berlin`), or None when the symlink is absent or names no zone."""
    try:
        target = str(path.readlink())
    except OSError:
        return None
    if _ZONEINFO_MARKER not in target:
        return None
    key = target.rsplit(_ZONEINFO_MARKER, 1)[1]
    try:
        ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return key


def schedule_zone_line(path: Path = LOCALTIME) -> str:
    """The `doctor` line: the zone schedules are read in, or that UTC stands in for one."""
    zone = host_zone_name(path)
    if zone is not None:
        return zone
    return f"UTC (fallback: {path} names no zone)"


#: The times both surfaces offer as one press, each a local wall-clock time from now.
TIME_PRESETS = ("in_1h", "tonight_0300", "tomorrow_0900")

#: The repeats both surfaces offer. `days` is followed by a choice of weekdays.
REPEATS = ("once", "daily", "weekdays", "days")


def local_now(now: datetime, zone: tzinfo) -> datetime:
    """`now` on the zone's wall clock, naive and to the minute -- what a schedule is set in."""
    return now.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0)


def preset_time(key: str, now: datetime, zone: tzinfo) -> datetime:
    """The local wall-clock time a preset names, measured from `now`."""
    local = local_now(now, zone)
    if key == "in_1h":
        return local + timedelta(hours=1)
    if key == "tonight_0300":
        night = datetime.combine(local.date(), time(3, 0))
        return night if night > local else night + timedelta(days=1)
    if key == "tomorrow_0900":
        return datetime.combine(local.date() + timedelta(days=1), time(9, 0))
    raise ValueError(f"no such time preset: {key}")


_CLOCK = re.compile(r"(\d{1,2}):(\d{2})")
_DATED = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})")


def parse_time_text(text: str, now: datetime, zone: tzinfo) -> datetime | None:
    """A typed time as a local wall-clock time, or None when it is not one.

    `HH:MM` is its next occurrence -- today if still ahead, else tomorrow; `YYYY-MM-DD HH:MM` is
    that date. Whether it is in the past is the caller's question (a one-shot refuses it).
    """
    typed = text.strip()
    clock = _CLOCK.fullmatch(typed)
    if clock is not None:
        hour, minute = int(clock[1]), int(clock[2])
        if hour > 23 or minute > 59:
            return None
        local = local_now(now, zone)
        today = datetime.combine(local.date(), time(hour, minute))
        return today if today > local else today + timedelta(days=1)
    dated = _DATED.fullmatch(typed)
    if dated is not None:
        try:
            return datetime(*(int(part) for part in dated.groups()))
        except ValueError:
            return None
    return None


def when_from(local: datetime, repeat: str, days: Iterable[Weekday] = ()) -> When:
    """The schedule's `when` from a chosen local time and repeat: a repeat keeps the time of
    day alone. Raises ValueError for `days` with none chosen, as `Repeat` does."""
    if repeat == "once":
        return Once(local)
    if repeat == "daily":
        return Repeat.daily(local.time())
    if repeat == "weekdays":
        return Repeat.weekdays(local.time())
    if repeat == "days":
        return Repeat(frozenset(days), local.time())
    raise ValueError(f"no such repeat: {repeat}")
