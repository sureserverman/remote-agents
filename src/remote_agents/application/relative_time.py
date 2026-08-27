"""How old a thing is, in the unit a reader would have used.

Here rather than in a driver adapter for the reason DEC-001 puts anything here: **both**
surfaces render this string, and until now both carried their own byte-identical copy of it —
`adapters/tui/model.py` and `adapters/telegram/service.py`. Two copies of one sentence is not
a problem while neither changes; it becomes one the moment a task says "humanize the age" and
names a single file, at which point the surfaces silently start describing the same session
differently. The bot's copy had no test at all, so nothing would have said so.

**The units deliberately do not compose.** A five-hour-old session reads `5h ago`, not
`5h 12m ago`. The extra precision is real and worthless: nobody reading a session list is
deciding anything on the twelve minutes, and the compound form reintroduces the exact
complaint the minutes-only version had, which is that the eye has to parse a number before it
knows roughly how old the thing is. It also keeps this function's own gate check honest — the
check greps for a bare `m ago` surviving into an hours-or-days age, and a compound form would
pass it while being the thing it was written to catch.
"""

from __future__ import annotations

from datetime import UTC, datetime

_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR


def age(created_at: datetime) -> str:
    """Render the time since `created_at` in whole minutes, hours, or days.

    Clamped at zero rather than rendering a negative age: a clock adjustment between the two
    surfaces' hosts, or a record written a moment in the future, should read as `0m ago` and
    not as `-3m ago`, which looks like a bug in the session rather than in the clock.
    """
    elapsed = max(0, int((datetime.now(UTC) - created_at).total_seconds()))
    if elapsed < _HOUR:
        return f"{elapsed // _MINUTE}m ago"
    if elapsed < _DAY:
        return f"{elapsed // _HOUR}h ago"
    return f"{elapsed // _DAY}d ago"


def until(moment: datetime) -> str:
    """Render the time remaining before `moment` in the same units, and the same way, as `age`.

    The mirror of `age`, and deliberately its exact mirror: same thresholds, same refusal to
    compound units, same clamp at zero. A rate-limit window that has already reset reads
    `0m` rather than a negative remainder, for the reason the clamp above exists — a clock the
    reader cannot see disagreeing by a second should not look like a broken session.

    Separate from `age` rather than a signed variant of it, because the two are read in
    opposite directions and the caller should not have to know which sign means which. The
    shared thresholds are the point of them living side by side.
    """
    remaining = max(0, int((moment - datetime.now(UTC)).total_seconds()))
    if remaining < _HOUR:
        return f"{remaining // _MINUTE}m"
    if remaining < _DAY:
        return f"{remaining // _HOUR}h"
    return f"{remaining // _DAY}d"
