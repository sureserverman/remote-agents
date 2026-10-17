"""Codex's limit sentence: the stop it prints into its pane, and the retry instant it names.

Read out of the installed binary (`docs/acceptance-2026-09-28-limit-screens.md`, codex-cli
0.158.0): `You’ve hit your usage limit.` -- a U+2019 apostrophe -- optionally followed by a way
round it (another model, an upgrade, the usage page) and by ` Try again at <time>.` or
` or try again at <time>.`, the time formatted `%b %-d, %Y %-I:%M %p` in the host's own zone.
Codex fires no hook on a failed turn, so this sentence on the pane is the only place its stop is
visible; the limit-screen watch matches it there.

**The day may carry an ordinal.** codex-cli 0.163.0 writes `Oct 18th, 2026 2:52 AM` (its binary
holds the `st`/`nd`/`rd`/`th` suffixes; every dated stop on record since 2026-09-30 has one), so
the suffix is optional rather than required: the 0.158.0 spelling still reads.

**A reset later the same day is a time alone** (`try again at 8:54 PM.`, `%-I:%M %p`, measured on
0.163.0). Codex prints it that way only when the reset falls on the day it prints, so the day is
the host's date at the moment the sentence is read, and a time already past stays on that day.
Not Claude's "next occurrence": rolled to tomorrow, a reset read a moment after its minute would
hold a lifted stop for a day whenever the reading is too stale to say otherwise. The accepted cost
is a sentence printed just before local midnight and read just after it, which lands a day late.

**Both apostrophes are matched.** The binary spells U+2019, and the one live sighting on record
was transcribed with an ASCII one; a marker that matched only one would miss the other silently.

**The sentence never names the window.** Only the reading can, so the hint answers `None` for it
and keeps the retry instant, which is still the one the lift waits for.
"""

from __future__ import annotations

import re
from datetime import datetime

from remote_agents.ports.agent_activity import LimitHit
from remote_agents.ports.limit_screen import LimitScreen

#: Anchored at the start of a line, led by nothing but indentation and the `■` Codex draws before
#: an error -- never its composer's `›` or an answer's `•` -- so a sentence the owner typed or an
#: answer quoting it is not a stop.
_STOP = r"^[ \t]*(?:■[ \t]*)?You[’']ve hit your usage limit"
_RETRY = re.compile(
    r"[Tt]ry again at (?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2})(?:st|nd|rd|th)?, (?P<year>\d{4})"
    r" (?P<hour>\d{1,2}):(?P<minute>\d{2}) (?P<meridiem>[AP]M)"
)
_SAME_DAY = re.compile(r"[Tt]ry again at (?P<hour>\d{1,2}):(?P<minute>\d{2}) (?P<meridiem>[AP]M)")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _hint(text: str, now: datetime) -> LimitHit | None:
    if re.search(_STOP, text, re.MULTILINE) is None:
        return None
    found = _RETRY.search(text)
    if found is None:
        return _same_day(text, now)
    month, day, year, hour, minute, meridiem = found.group(
        "month", "day", "year", "hour", "minute", "meridiem"
    )
    if month not in _MONTHS:
        return LimitHit(None, None)
    try:
        # Read with a fixed month table rather than `strptime`'s `%b`/`%p`, which follow the
        # service's locale, not Codex's English. A naive local reading placed in the host's
        # zone: Codex formats it with the host's clock, and `astimezone` on a naive value is
        # exactly "this is local time".
        local = datetime(
            int(year),
            _MONTHS.index(month) + 1,
            int(day),
            int(hour) % 12 + (12 if meridiem == "PM" else 0),
            int(minute),
        )
        return LimitHit(None, local.astimezone())
    except ValueError:
        return LimitHit(None, None)


def _same_day(text: str, now: datetime) -> LimitHit:
    found = _SAME_DAY.search(text)
    if found is None:
        return LimitHit(None, None)
    hour, minute, meridiem = found.group("hour", "minute", "meridiem")
    today = now.astimezone().date()
    try:
        # Naive and local, then placed in the host's zone, exactly as the dated form is.
        local = datetime(
            today.year,
            today.month,
            today.day,
            int(hour) % 12 + (12 if meridiem == "PM" else 0),
            int(minute),
        )
        return LimitHit(None, local.astimezone())
    except ValueError:
        return LimitHit(None, None)


LIMIT_SCREEN = LimitScreen(markers=(_STOP,), hint=_hint)
