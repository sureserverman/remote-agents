"""Whether a limit stop has lifted, decided once for the bot's line and the "carry on" after it.

A lift has two witnesses. **The schedule:** the provider published when the window resets, and
that instant, plus a minute of grace, has passed. **A reading:** the provider's own accounting,
taken after the stop, shows the window no longer full -- which is how an early or external reset
is seen, and how a stop whose instant nobody published lifts at all.

**It fails toward "not yet".** A false lift retires the owner's limit line while the agent is
still stopped and, with the resume switch on, types "carry on" into it. A late lift costs a few
minutes of a line the owner can already see. So each witness is admitted only on terms that
cannot mislead:

- A scheduled lift is held back by a reading taken *after* the reset that still shows the window
  full: the provider moved its own reset. A reading from before the reset says nothing about it.
- A reading lifts only when it was taken after the stop, is not stale by `session_views`' rule
  (asked, not restated, DEC-043), and names the stop's own window below full. A different window,
  an absence, a reading with no stamp or with a naive one is not evidence of anything.
- A stop whose window is unknown lifts from a reading only when every window it carries is below
  full, and never on a schedule nobody published.
- A Claude model week (`opus week`, ...) is a label no reading publishes, so it lifts on its own
  reset only.

"Full" is `limit_stops`' saturation line, so the window that named the stop and the window that
lifts it are measured by the same figure.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from remote_agents.application.limit_stops import _SATURATED
from remote_agents.application.session_views import _STALE_READING_AGE
from remote_agents.ports.agent_activity import LimitHit
from remote_agents.ports.agent_usage import AgentLimits, UsageWindow

LIFT_GRACE = timedelta(seconds=60)
"""How long past a published reset the schedule alone waits before it calls the stop lifted.

Providers roll their counters over on their own clock, and this host's clock is not theirs; a
minute is the margin that keeps "carry on" from landing a few seconds before the window opens.
"""


def lifted(
    hit: LimitHit,
    stopped_at: datetime,
    reading: AgentLimits | None,
    *,
    now: datetime,
) -> bool:
    """Whether the stop recorded as `hit` at `stopped_at` has lifted by `now`. Total and pure."""
    fresh = _fresh(reading, now=now)
    resets_at = hit.resets_at if _aware(hit.resets_at) else None
    if resets_at is not None and now >= resets_at + LIFT_GRACE:
        held = (
            fresh is not None and fresh.observed_at is not None and fresh.observed_at >= resets_at
        )
        if not (held and _still_full(fresh.windows, hit.window)):
            return True
    if fresh is None or fresh.observed_at is None or not _aware(stopped_at):
        return False
    if fresh.observed_at <= stopped_at:
        return False
    if hit.window is None:
        return bool(fresh.windows) and not _still_full(fresh.windows, None)
    own = [window for window in fresh.windows if window.label == hit.window]
    return bool(own) and all(window.used_percent < _SATURATED for window in own)


def _fresh(reading: AgentLimits | None, *, now: datetime) -> AgentLimits | None:
    """The reading, when it can be evidence at all: present, stamped with a zone, not stale."""
    if reading is None or reading.absence is not None or reading.observed_at is None:
        return None
    if not _aware(reading.observed_at) or now - reading.observed_at > _STALE_READING_AGE:
        return None
    return reading


def _still_full(windows: tuple[UsageWindow, ...], label: str | None) -> bool:
    """Whether `label`'s window -- or, for an unknown window, any window -- is still full."""
    return any(
        window.used_percent >= _SATURATED and (label is None or window.label == label)
        for window in windows
    )


def _aware(instant: datetime | None) -> bool:
    return instant is not None and instant.tzinfo is not None
