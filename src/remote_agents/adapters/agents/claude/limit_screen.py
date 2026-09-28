"""Claude Code's limit sentence: which window it names, and when it says the window resets.

Read out of the installed bundle rather than guessed (`docs/acceptance-2026-09-28-limit-screens.md`,
claude 2.1.284). One function builds the sentence -- `You've hit your <label>`, then
` · resets <time> (<zone>)`, then an optional ` · progress saved` -- and one table names the
label: `five_hour` is "session limit", `seven_day` is "weekly limit", and the per-model weeks are
"Opus limit", "Sonnet limit" and "Fable limit". The time is `10:50am` when the reset is within a
day and `Sep 30, 9am` beyond one, with the year inserted when it is not the current one; the zone
is the IANA name Claude's host resolved.

**The model weeks get labels no reading publishes, on purpose.** The account reading carries only
`5h` and `week`, so an Opus limit filed as `week` would be "lifted" by an overall weekly figure
that was never the one stopping the agent. A label of its own can only be lifted by its own
instant.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from remote_agents.ports.agent_activity import LimitHit
from remote_agents.ports.limit_screen import LimitScreen

_STOP = re.compile(
    r"You've hit your (?:(?P<label>session|weekly|Opus|Sonnet|Fable|usage credit) )?limit"
)

#: Claude's words for its windows, onto the labels the limits readers publish (`5h`, `week`) or,
#: for a window no reading carries, a label of its own. "usage credit" is a spend ceiling, not a
#: window, so it names none.
_WINDOWS: dict[str | None, str | None] = {
    "session": "5h",
    "weekly": "week",
    "Opus": "opus week",
    "Sonnet": "sonnet week",
    "Fable": "fable week",
    "usage credit": None,
    None: None,
}

_RESETS = re.compile(
    r" · resets "
    r"(?:(?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2}), (?:(?P<year>\d{4}), )?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?P<meridiem>am|pm)"
    r" \((?P<zone>[^()]+)\)"
)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _hint(text: str, now: datetime) -> LimitHit | None:
    stop = _STOP.search(text)
    if stop is None:
        return None
    return LimitHit(_WINDOWS[stop.group("label")], _resets_at(text[stop.end() :], now))


def _resets_at(text: str, now: datetime) -> datetime | None:
    """The instant a ` · resets …` clause names, placed on its date, or `None`."""
    found = _RESETS.search(text)
    if found is None:
        return None
    try:
        zone = ZoneInfo(found.group("zone"))
    except (ZoneInfoNotFoundError, ValueError):
        return None
    hour = int(found.group("hour")) % 12 + (12 if found.group("meridiem") == "pm" else 0)
    minute = int(found.group("minute") or 0)
    local_now = now.astimezone(zone)
    try:
        if found.group("month") is None:
            # Inside a day: the next time the clock reads this, which is tomorrow's when today's
            # has already gone.
            candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return candidate if candidate > local_now else candidate + timedelta(days=1)
        month = _MONTHS.index(found.group("month")) + 1
        year = int(found.group("year") or local_now.year)
        return datetime(year, month, int(found.group("day")), hour, minute, tzinfo=zone)
    except ValueError:
        return None


LIMIT_SCREEN = LimitScreen(markers=(r"You've hit your (?:[A-Za-z]+ )*limit",), hint=_hint)
"""Claude reports the stop through its `StopFailure` hook, so no pane watch matches `markers`;
the hint is what reads the hook's detail."""
