"""Which usage window stopped a session, decided once for every place that records a stop.

A limit stop reaches this project three ways -- Claude's `StopFailure` hook, and the limit screen
Codex and Cursor Agent draw in their panes -- and each arrives knowing less than the owner wants
told: the hook says "rate limit", the screen says "you've hit your usage limit", and only
sometimes does the provider's own sentence name the window. So the window is decided here, from
two pieces of evidence the caller hands over, and never re-decided by a notifier or a feed row
(DEC-043).

**The measured figure beats the sentence.** A live reading showing a window at 100% is the
provider's own accounting at the moment of the stop; a hint parsed out of a sentence is the
provider's wording, which has changed before (Claude says "session limit" for its 5-hour window).
When they disagree the reading names the window, and the hint only fills an instant the reading
did not publish for that same window.

**When two windows are full, the later reset is what keeps the agent stopped.** A week window at
100% with a 5-hour window also at 100% lifts only when the week does, and the reset instant is
what the lift and the "carry on" after it wait for.

**Unknown is an answer, not a guess.** A stale reading, an absence, and no hint all fall through
to `LimitHit(None, None)`: the stop is still a stop, and every surface keeps its window-blind
wording for it. The same direction `limit_resets` fails in, for the same reason -- a wrong
"weekly limit" would send the owner to wait days for something that lifts in hours.
"""

from __future__ import annotations

from datetime import datetime

from remote_agents.application.session_views import _STALE_READING_AGE
from remote_agents.ports.agent_activity import LimitHit
from remote_agents.ports.agent_usage import AgentLimits, UsageWindow

#: The percentage at which a window counts as the one that stopped the agent. Both providers
#: publish integers and report exactly 100 at the limit; anything lower is a window with room.
_SATURATED = 100.0


def classify(
    reading: AgentLimits | None,
    hint: LimitHit | None,
    *,
    now: datetime,
) -> LimitHit:
    """The window that stopped a session, from its provider's reading and its own sentence.

    Total: no input raises. `reading` is the provider's current account reading (or `None` when
    none was available); `hint` is what the provider's vertical parsed out of its own limit text.
    """
    saturated = _saturated(reading, now=now)
    if saturated is not None:
        resets_at = saturated.resets_at
        if resets_at is None and hint is not None and hint.window == saturated.label:
            resets_at = hint.resets_at
        return LimitHit(saturated.label, resets_at)
    if hint is not None:
        return hint
    return LimitHit(None, None)


def _saturated(reading: AgentLimits | None, *, now: datetime) -> UsageWindow | None:
    """The full window that lifts last, or `None` when the reading cannot name one.

    Staleness is `session_views`' own rule, asked rather than restated (DEC-043): a reading the
    surfaces would date is a reading this rule will not trust to name a window.
    """
    if reading is None or reading.absence is not None:
        return None
    if reading.observed_at is not None and now - reading.observed_at > _STALE_READING_AGE:
        return None
    full = [window for window in reading.windows if window.used_percent >= _SATURATED]
    if not full:
        return None
    return max(full, key=_lifts_last)


def _lifts_last(window: UsageWindow) -> tuple[bool, datetime | float]:
    """Order full windows by when they lift; one with no published instant sorts first."""
    if window.resets_at is None:
        return (False, 0.0)
    return (True, window.resets_at)
