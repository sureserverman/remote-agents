"""Type one "carry on" into a session whose limit lifted -- once, and only where it is safe.

The owner asked for this on 2026-09-28: a session a limit stopped sits idle in its pane after the
window reopens, and nothing restarts it. So when the lift pass (`limit_lifts`) decides a stop has
lifted, and the Settings switch is on (its default), the service types one fixed message into the
session's composer. This is the only place that message is spelled.

**Through the terminal's guarded send, never through the owner's relay queue.** The send keeps
every DEC-099 guard -- an empty idle composer only, never into a dialog, `Enter` only after the
draft is seen. What it does not do is queue: `PromptRelay.submit` queues a message refused as
busy, and a queued nudge would replace the owner's own waiting message and fire after some later
turn, out of context. So a refusal is decided here instead:

- **busy** (or another sender at the keys, or tmux failing before anything was typed) is tried
  again each pass, for `BUSY_PATIENCE`, then given up;
- **a dialog, a draft in the composer, a screen not recognised** are given up at once -- waiting
  does not fix them, and typing past them is what the guards exist to stop;
- **unconfirmed** (typed, and not seen to land) is never tried again: a double submit is worse
  than a lost one (DEC-099).

**At most one nudge per stop, and it survives a restart**: the lift pass writes a `NUDGING`
intent before the typing and the outcome over it before it touches the bot's line
(`limit_lifts.LimitLiftWatcher`); an intent found after a restart is given up as unconfirmed.
The busy clock is this process's memory only, so a restart during a busy wait starts the wait
again -- it can delay the give-up, never repeat a nudge.

**Cursor is not resumed by this, and is never asked.** Its limit screen keeps the owner's own
message in the composer (`LimitScreen.keeps_draft`), so the lift pass retires a lifted Cursor
stop without coming here (DEC-110). Submitting that kept draft is a different pane action, and
it is not built (BL-112).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from remote_agents.application.prompt_delivery import DeliveryVerdict, delivery_verdict
from remote_agents.domain.models import SessionId
from remote_agents.ports.limit_stop_outcomes import (
    LIFTED,
    NOT_RESUMED,
    RESUMED,
    LimitStop,
    LimitStopOutcomes,
)
from remote_agents.ports.terminal import PromptDelivery

__all__ = [
    "BUSY_PATIENCE",
    "LOOP_GUARD",
    "NUDGE",
    "RESUMED",
    "LimitResume",
    "Nudge",
    "NotResumed",
    "not_resumed",
]

_LOG = logging.getLogger(__name__)

NUDGE = "carry on"
"""The one message the service types, spelled here and nowhere else."""

BUSY_PATIENCE = timedelta(minutes=30)
"""How long a busy pane is tried again, each pass, before the nudge is given up."""

LOOP_GUARD = timedelta(minutes=10)
"""A stop this soon after a nudge is lifted by its own published reset only.

A nudge into a window that had not really reopened stops the agent again at once. A reading
might then call that new stop lifted early, and nudge it, and so on. Waiting for the published
reset breaks the loop, and a stop with no published reset is never lifted at all.
"""

OUTCOME_RETENTION = timedelta(days=90)
"""How long a finished limit stop's outcome is kept (BL-111, owner ruling 2026-10-08).

The table gains a row per stop and nothing else deletes one. A `NUDGING` intent is never pruned,
because it is what stops a nudge being typed twice (DEC-099), and neither is the outcome of a
session that can still act, because a stop whose outcome is missing reads as undecided again."""


async def prune_outcomes(outcomes: LimitStopOutcomes, *, now: datetime) -> int:
    """Remove finished outcomes older than `OUTCOME_RETENTION`, answering how many went."""
    return await outcomes.prune(now - OUTCOME_RETENTION)


class NotResumed(StrEnum):
    """Why a lifted stop was not nudged -- a key the bot words, never shown as itself."""

    BUSY = "busy"
    DIALOG = "dialog"
    COMPOSING = "composing"
    UNRECOGNISED = "unrecognised"
    UNCONFIRMED = "unconfirmed"


def not_resumed(reason: NotResumed) -> str:
    """The outcome recorded for a stop that was not nudged."""
    return f"{NOT_RESUMED}:{reason.value}"


@dataclass(frozen=True, slots=True)
class Nudge:
    """What one attempt came to.

    `outcome` is `None` while the attempt should be made again next pass (busy, inside the
    patience), `LIFTED` when there was no session to nudge after all, and otherwise the outcome
    to record -- with `reason` set when it is not `RESUMED`.
    """

    outcome: str | None
    reason: NotResumed | None = None


class LimitResume:
    """The resume step the lift pass calls for a stop that lifted.

    Everything outside the application is handed in: the terminal's guarded send, the Settings
    switch, and the bot's two answers about the stop's line -- whether it is settled (sent, and
    the chat not held), and the amendment that says a nudge was not sent.
    """

    def __init__(
        self,
        *,
        send: Callable[[SessionId, str], Awaitable[PromptDelivery]],
        enabled: Callable[[], Awaitable[bool]],
        settled: Callable[[LimitStop], Awaitable[bool]],
        amend: Callable[[LimitStop, str], Awaitable[bool]],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._send = send
        self._enabled = enabled
        self.settled = settled
        self.amend = amend
        self._now = now
        #: When each stop was first found busy, keyed by its session and stamp.
        self._busy_since: dict[tuple[str, str], datetime] = {}
        #: Each stop's final verdict until its outcome is recorded. A record that failed is
        #: tried again with this verdict, never with a second send.
        self._decided: dict[tuple[str, str], Nudge] = {}

    async def enabled(self) -> bool:
        """The switch, read afresh. A read that fails counts as off: never type on a doubt."""
        try:
            return bool(await self._enabled())
        except Exception:
            _LOG.warning("the resume switch could not be read; nothing is typed this pass")
            return False

    async def nudge(self, stop: LimitStop) -> Nudge:
        """Type the nudge into the stop's session, and say what that came to.

        Once a stop has a final verdict it is answered from memory until `forget` says its
        outcome was recorded, so this process never types into the same stop twice.
        """
        key = (stop.session_id, stop.stamp)
        decided = self._decided.get(key)
        if decided is not None:
            return decided
        verdict = await self._attempt(stop, key)
        if verdict.outcome is not None:
            self._decided[key] = verdict
        return verdict

    def holds(self, stop: LimitStop) -> bool:
        """Whether a final verdict for this stop is waiting to be recorded."""
        return (stop.session_id, stop.stamp) in self._decided

    def held(self, stop: LimitStop) -> Nudge | None:
        """The final verdict waiting to be recorded for this stop, if there is one."""
        return self._decided.get((stop.session_id, stop.stamp))

    def forget(self, stop: LimitStop) -> None:
        """The stop's outcome is recorded; its verdict need not be held any longer."""
        self._decided.pop((stop.session_id, stop.stamp), None)

    async def _attempt(self, stop: LimitStop, key: tuple[str, str]) -> Nudge:
        try:
            delivery = await self._send(SessionId.parse(stop.session_id), NUDGE)
        except Exception:
            # The send never raises by contract; a raise here may have typed, so it is given
            # up rather than tried again.
            _LOG.exception("the nudge failed partway; it is not tried again")
            self._busy_since.pop(key, None)
            return _given_up(NotResumed.UNCONFIRMED)
        verdict = delivery_verdict(delivery)
        if verdict is DeliveryVerdict.SENT:
            self._busy_since.pop(key, None)
            return Nudge(RESUMED)
        if verdict is DeliveryVerdict.UNCONFIRMED:
            self._busy_since.pop(key, None)
            return _given_up(NotResumed.UNCONFIRMED)
        if verdict is DeliveryVerdict.NOT_RUNNING:
            self._busy_since.pop(key, None)
            return Nudge(LIFTED)
        if verdict is DeliveryVerdict.WAIT:
            now = self._now()
            since = self._busy_since.setdefault(key, now)
            if now - since < BUSY_PATIENCE:
                return Nudge(None)
            self._busy_since.pop(key, None)
            return _given_up(NotResumed.BUSY)
        self._busy_since.pop(key, None)
        if verdict is DeliveryVerdict.DIALOG:
            return _given_up(NotResumed.DIALOG)
        if verdict is DeliveryVerdict.COMPOSING:
            return _given_up(NotResumed.COMPOSING)
        # An unrecognised screen, and a refusal of the text or the agent, which "carry on" never
        # earns: neither is one that waiting fixes.
        return _given_up(NotResumed.UNRECOGNISED)


def _given_up(reason: NotResumed) -> Nudge:
    return Nudge(not_resumed(reason), reason)
