"""One place that remembers when Telegram said this chat may be spoken to again.

Flood control is the *chat's* state, not one call's, so every sender has to honour the same
answer. It was owned by nobody: the sessions redraw, the command handlers and the activity
notifier each discovered a ban separately and each kept going, which is how a ten-second
cooldown on 2026-09-13 became a ban of nearly six hours.

Deliberately not a rate limiter. It answers one question -- may I send yet -- and the only
thing that ever sets it is Telegram's own `retry_after`.
"""

from __future__ import annotations

from time import monotonic

__all__ = ["FloodGate"]


class FloodGate:
    """The moment the chat may be spoken to again, shared by every sender."""

    def __init__(self) -> None:
        self._until = 0.0

    def hold_off(self, seconds: float) -> None:
        """Refuse sends for `seconds`. Only ever extends.

        A shorter answer arriving while a longer hold stands is not permission to speak
        sooner: during an escalating ban the *later* replies carry the smaller remainder, so
        taking the newest would walk the gate back down into the ban that set it.
        """
        self._until = max(self._until, monotonic() + max(seconds, 0.0))

    def held(self) -> bool:
        """Whether Telegram has told us to wait, and the wait has not yet run out."""
        return monotonic() < self._until

    def remaining(self) -> float:
        """Seconds left on the hold; zero when there is none."""
        return max(self._until - monotonic(), 0.0)
