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

**Unknown is an answer, not a guess.** A stale reading or an absence falls through to the hint,
and with no hint either the answer is `LimitHit(None, None)`: the stop is still a stop, and
every surface keeps its window-blind wording for it. A full window with no published instant is
treated as the last to lift, because resuming on the other window's reset would type into an
agent that is still stopped. The same direction `limit_resets` fails in, for the same reason --
a wrong "weekly limit" would send the owner to wait days for something that lifts in hours.

**An instant without a zone is not evidence.** It can be neither compared with `now` nor stored
without guessing the host's offset, so a reading stamped with one is not read, a window carrying
one is skipped, and a hint's naive instant is dropped while its window is kept. That is what makes
the function total: nothing here can raise on a reading it did not expect.
"""

from __future__ import annotations

from datetime import datetime

from remote_agents.application.session_views import _STALE_READING_AGE
from remote_agents.ports.agent_activity import LimitHit
from remote_agents.ports.agent_usage import AgentLimits, UsageWindow

#: The percentage at which a window counts as the one that stopped the agent: anything that rounds
#: to 100. Readers pass the provider's figure through unrounded, and nothing guarantees a blocked
#: window reads exactly 100, so a window the surfaces would draw as "100%" is a full one here too.
_SATURATED = 99.5


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
    if hint is not None and not _aware(hint.resets_at):
        hint = LimitHit(hint.window, None)
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
    surfaces would date is a reading this rule will not trust to name a window. A full window
    whose own reset has already passed is not what stops the agent now, so it names nothing.
    """
    if reading is None or reading.absence is not None:
        return None
    if reading.observed_at is not None:
        if not _aware(reading.observed_at) or now - reading.observed_at > _STALE_READING_AGE:
            return None
    full = [
        window
        for window in reading.windows
        if window.used_percent >= _SATURATED
        and _aware(window.resets_at)
        and (window.resets_at is None or window.resets_at > now)
    ]
    if not full:
        return None
    return max(full, key=_lifts_last)


def _lifts_last(window: UsageWindow) -> tuple[int, datetime | None]:
    """Order full windows by when they lift; one with no published instant lifts last.

    Equal instants keep the reading's own order (`max` returns the first of equals).
    """
    if window.resets_at is None:
        return (1, None)
    return (0, window.resets_at)


def _aware(instant: datetime | None) -> bool:
    """Whether an instant is absent or carries a zone -- the two shapes this rule can use."""
    return instant is None or instant.tzinfo is not None
