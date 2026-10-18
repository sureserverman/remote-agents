"""The one sentence the owner reads when a provider wiped its meters ahead of schedule.

`Claude limits were reset early — 5h 91% → 2%, week 64% → 0%` — one message per provider per
event (DEC-097), naming every window that moved and what it moved from.

**Nothing here decides whether a reset happened.** `application/limit_resets.py` holds that
rule and holds it once (DEC-043, DEC-007); this module is handed the verdict and writes it down.
The division matters because the figure cannot be checked afterwards — by the time the owner
reads this, the window that was wiped is simply a window that has been wiped — so the sentence
and the judgement must not be able to disagree.

**The provider's display name is an argument, not a lookup, and that is load-bearing.** This
project already names providers in one place; spelling "Claude" here would make a second, free
to drift from the first. Passing it in makes the property structural rather than remembered —
the module *cannot* name a provider — and
`test_limit_reset_present_spells_no_provider_name_of_its_own` asserts exactly that over this
file's own source.

**Escaping happens here, at the boundary that decides the markup (DEC-014).** The window labels
are the provider's own text and the display name is resolved elsewhere, so neither is assumed
safe: an unbalanced `<` in either reaches a surface that parses HTML, which is a rendering fault
this project has already paid for once. `application/session_views.py` deliberately takes no
view on either surface's markup, which is why it is not done there.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from html import escape

from telegram.constants import ParseMode

from remote_agents.adapters.telegram.expiring_notices import ExpiringMessages
from remote_agents.adapters.telegram.flood import FloodGate
from remote_agents.application.early_reset_setting import (
    DEFAULT_EARLY_RESET_NOTICE,
    early_reset_lifetime,
)
from remote_agents.application.limit_resets import EarlyReset, detect
from remote_agents.application.notification_policy import REFUSALS_BEFORE_ABANDONING
from remote_agents.application.session_views import whole_percent
from remote_agents.ports.agent_usage import AgentLimits
from remote_agents.ports.expiring_notices import ExpiringNoticeStore

_LOG = logging.getLogger(__name__)

#: The kind its sent messages are remembered under (`ports.expiring_notices`).
NOTICE_KIND = "limit-reset"


def limit_reset_message(provider: str, resets: tuple[EarlyReset, ...]) -> str:
    """The sentence for one provider's early reset, or nothing when nothing reset.

    The empty answer is a real one rather than a guard against a caller mistake: the notifier
    asks per provider on every pass and most passes have nothing to say, and a headline with no
    windows under it would be an interruption reporting that nothing happened — which is the
    half of DEC-031 this kind was admitted under, not an exception to it.

    Percentages go through `whole_percent`, the same rule the limits block rounds with. A
    provider is free to publish `90.6`, and a figure rendered one way in a message and another
    way two screens along is two opinions about one number (DEC-043).
    """
    if not resets:
        return ""
    moved = ", ".join(
        f"{escape(reset.label)} {whole_percent(reset.previous_percent)}%"
        f" → {whole_percent(reset.current_percent)}%"
        for reset in resets
    )
    return f"{escape(provider)} limits were reset early — {moved}"


class LimitResetNotifier:
    """Read each provider's limits on a clock, and say once when one of them was wiped early.

    The trust notifier's shape rather than the activity notifier's — its own pass, driven by a
    loop of its own, holding what it could not deliver — and the first notifier here whose
    subject is **not a session**. DEC-031 required one; DEC-097 amends it for this kind alone,
    on the test DEC-031 itself set: a wiped meter changes what the owner can do next.

    **Limits are reached through one argumentless callable and nothing else.** That is
    `Backend.limits`, the read both surfaces already share, and it is handed in (DEC-046). The
    consequence is the point: this class cannot choose a source, so the owner's Settings choice
    and DEC-087's opt-in remain the only things that decide where a figure came from. It
    imports no provider reader, and the Stage 2 gate greps for exactly that.

    **The baseline is in memory and moves on every pass it can read**, which is what makes a
    wipe news once. Persisting it was offered to the owner and declined; the accepted cost is
    that an early reset in the minutes around a restart is missed. Missed, never invented.

    **What is retried is the message, not the detection.** Once the baseline has moved the same
    wipe can never be found again, so a refusal that dropped the sentence would drop it
    permanently — it is held and re-sent instead, and three consecutive refusals abandon it with
    one journal line. A chat that cannot be reached is evidence about the chat, not about this
    provider.

    **A sent message deletes itself** after the lifetime the owner chose in Settings (1 or 6
    hours), and a newer reset for the same provider replaces it. With the choice at off the
    watch still moves its baseline and journals what it found, and sends nothing.
    """

    def __init__(
        self,
        *,
        limits: Callable[[], Awaitable[Sequence[AgentLimits]]],
        view: object,
        name_for: Callable[[str], str],
        flood: FloodGate | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        choice: Callable[[], Awaitable[str]] | None = None,
        notices: ExpiringNoticeStore | None = None,
    ) -> None:
        self._limits = limits
        self._view = view
        #: How a provider is named for the owner. Handed in for the reason the module docstring
        #: gives: naming one here would be a second naming site, free to drift from the first.
        self._name_for = name_for
        #: The chat-wide "not yet", shared with every other sender, or a private one where a
        #: composition wired none — the same default the activity pass takes, and for the same
        #: reason: three senders that each discovered a ban separately turned a ten-second
        #: cooldown into a ban of nearly six hours.
        self._flood = flood if flood is not None else FloodGate()
        self._now = now
        #: The owner's early-reset choice, read on every pass; the default where none is wired.
        self._choice = choice
        self._messages = ExpiringMessages(view=view, kind=NOTICE_KIND, store=notices)
        self._bot: object | None = None
        #: The last reading per provider, by profile id. Replaced on every readable pass.
        self._baseline: dict[str, AgentLimits] = {}
        #: A sentence detected but not yet delivered, per provider, with its refusal count.
        self._pending: dict[str, str] = {}
        self._refusals: dict[str, int] = {}
        #: Providers already given up on, so the journal line is written once rather than on
        #: every pass for as long as the process lives.
        self._abandoned: set[str] = set()

    def attach(self, bot: object) -> None:
        """Learn which Telegram application to speak through, once there is one."""
        self._bot = bot

    async def pass_once(self) -> None:
        """One tick: read, detect, move the baseline, then deliver whatever is outstanding.

        **Detection and delivery are separate halves on purpose.** The baseline has to move
        whether or not the chat is reachable — otherwise a flood ban would re-detect the same
        wipe on every pass and queue a message per tick — while the sentence has to survive a
        refusal, because by then it is the only remaining record that the wipe happened.

        Never raises. The loop that drives this runs beside reconcile, activity and trust, and
        a pass that raised on an unexpected reading would take the others down with it on a
        timer.
        """
        try:
            readings = await self._limits()
        except Exception:
            # Not a reading, so not a comparison. The baseline is kept exactly as it was: an
            # empty or failed read overwritten into it would be compared against the next real
            # one and read as a wipe from nothing.
            _LOG.debug("the limits read failed; this pass compares nothing", exc_info=True)
            readings = ()
        now = self._now()
        lifetime = await self._lifetime()
        if lifetime is None:
            # Off: nothing is told, and nothing detected earlier is still owed.
            self._pending.clear()
            self._refusals.clear()
        for reading in readings:
            key = str(reading.profile_id)
            previous = self._baseline.get(key)
            for_this_provider = detect(previous, reading, now=now)
            if previous is None:
                # **The one line a healthy watch prints, and it exists because the alternative
                # was unobservable.** A pass with nothing to report was silent, so on a real
                # host "the loop ran and found nothing" and "the loop was never created"
                # produced identical journals — the same ambiguity a surviving mutant exposed
                # in the unit tests, which the Stage 3 live gate then could not satisfy at all.
                #
                # Once per provider, at the moment its baseline is first taken. Not repeated:
                # a line per provider every five minutes forever is a journal nobody reads,
                # which is its own kind of unobservable.
                _LOG.info(
                    "limits watch: baseline taken for %s (%d window(s)); nothing to report",
                    key,
                    len(reading.windows),
                )
            self._baseline[key] = reading
            if not for_this_provider:
                continue
            # The record a claim nobody can check afterwards needs: what moved, from where, and
            # when the provider had said it would reset.
            _LOG.info(
                "early limits reset detected for %s (source %s): %s%s",
                key,
                reading.stale_source or "live",
                "; ".join(
                    f"{reset.label} {reset.previous_percent:g}% -> {reset.current_percent:g}%"
                    f" (was to reset {reset.expected_reset_at.isoformat()})"
                    for reset in for_this_provider
                ),
                "" if lifetime is not None else "; not told, the notice is off",
            )
            if lifetime is None or key in self._abandoned:
                continue
            # One message per provider per event, so a second detection while the first is
            # still undelivered replaces it rather than queueing beside it — the later sentence
            # is the truer one, and two notifications about one wipe is the shape DEC-031's
            # one-per-pass clause exists to prevent.
            self._pending[key] = limit_reset_message(self._name_for(key), for_this_provider)
            # **The new sentence starts its own count.** Refusals recorded against the event it
            # replaces are about a message nobody will ever send now; carried over, they would
            # let a fresh wipe be abandoned on its first real attempt at delivery, on the
            # strength of an unrelated earlier failure.
            self._refusals.pop(key, None)
        if lifetime is not None:
            await self._deliver(now + lifetime)
        if self._bot is not None:
            await self._messages.sweep(self._bot, now)

    async def _lifetime(self) -> timedelta | None:
        """How long a message sent this pass stays, or `None` when the owner turned it off."""
        if self._choice is None:
            return early_reset_lifetime(DEFAULT_EARLY_RESET_NOTICE)
        try:
            return early_reset_lifetime(await self._choice())
        except Exception:
            _LOG.debug("the early-reset choice could not be read; using the default", exc_info=True)
            return early_reset_lifetime(DEFAULT_EARLY_RESET_NOTICE)

    async def _deliver(self, expires_at: datetime) -> None:
        """Send what is outstanding, and let a pass that reached nobody say nothing about it.

        **A refusal is evidence about this message only when the channel is demonstrably
        working** — DEC-049's clause, and the reason the strikes are collected across the pass
        rather than counted as they happen. When at least one other provider's message got
        through, a refusal really is about the message that was refused: too long, bad markup,
        something this pass will never fix by repeating. When nothing got through, the refusals
        are evidence about an outage, and counting them would spend the whole budget on a
        Telegram hiccup.

        The arithmetic is why that matters more here than anywhere else in this adapter. At a
        300 s cadence three strikes is fifteen minutes; `_abandoned` is keyed on the **provider**
        rather than on a session, and nothing clears it — a session ends and takes its
        abandonment with it, a provider does not. So the first version of this method, which
        struck unconditionally, turned one ordinary outage into early-reset notifications being
        silently dead for that provider for the life of the process. It cited DEC-049 in its own
        docstring while not implementing it, which is worse than not citing it at all. Found by
        Stage 2's Tier-2 review, against `trust_notifications.py`, which has it right.
        """
        if self._bot is None or not self._pending:
            return
        refused: list[str] = []
        delivered = 0
        for key, text in list(self._pending.items()):
            if self._flood.held():
                # Re-asked per message, not once for the pass: a send that trips a fresh ban
                # must not be followed by another spending a request into the ban it just
                # caused. The siblings ask once; this asks each time, which costs nothing.
                _LOG.debug(
                    "limit-reset notifications held: %.0fs left on the chat's flood hold",
                    self._flood.remaining(),
                )
                break
            try:
                message_id = await self._view.send_apart(
                    self._bot, {"text": text, "parse_mode": ParseMode.HTML}
                )
            except Exception:
                refused.append(key)
                continue
            await self._messages.sent(self._bot, message_id, subject=key, expires_at=expires_at)
            self._pending.pop(key, None)
            self._refusals.pop(key, None)
            delivered += 1
        if not delivered:
            if refused:
                _LOG.info(
                    "no early-limits-reset message got through this pass (%d refused); "
                    "treating it as an outage rather than as evidence about any provider",
                    len(refused),
                )
            return
        for key in refused:
            self._refusals[key] = self._refusals.get(key, 0) + 1
            if self._refusals[key] >= REFUSALS_BEFORE_ABANDONING:
                self._abandoned.add(key)
                self._pending.pop(key, None)
                _LOG.warning(
                    "giving up on the early-limits-reset message for %s after %d refusals; "
                    "the figures themselves are on the limits screen",
                    key,
                    REFUSALS_BEFORE_ABANDONING,
                )
