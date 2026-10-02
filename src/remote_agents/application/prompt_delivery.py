"""What a guarded send's answer means for a caller deciding whether to try again (DEC-099).

The service types into a pane on its own in two places: the nudge after a limit lifts
(`limit_resume`, DEC-109) and a schedule's first prompt (`schedules`). Both read a refusal the
same way, so the reading lives here once (DEC-043); what each does with a verdict -- how long it
waits, what it gives up on -- is its own.
"""

from __future__ import annotations

from enum import StrEnum

from remote_agents.ports.terminal import PromptDelivery, PromptOutcome, PromptReason


class DeliveryVerdict(StrEnum):
    SENT = "sent"
    WAIT = "wait"
    """Nothing was typed, and a later try may land: busy, another sender at the keys, or tmux
    failing before the paste (a partway failure is UNCONFIRMED)."""
    NOT_RUNNING = "not_running"
    DIALOG = "dialog"
    """A dialog is up, or a command menu would run something other than what was typed."""
    COMPOSING = "composing"
    """The composer holds a draft already; typing would join it."""
    UNRECOGNISED = "unrecognised"
    """The screen is not one the agent's composer description recognises."""
    REFUSED = "refused"
    """The text or the agent was refused before anything was looked at: empty, a shell command,
    or an agent with no composer. Waiting never changes these."""
    UNCONFIRMED = "unconfirmed"
    """Something may have been typed and was not seen to land: never tried again."""


_WAITS = frozenset({PromptReason.BUSY, PromptReason.KEYS_BUSY, PromptReason.TMUX_ERROR})
_DIALOGS = frozenset({PromptReason.DIALOG, PromptReason.MENU})
_REFUSED = frozenset({PromptReason.EMPTY, PromptReason.SHELL, PromptReason.NO_COMPOSER})


def delivery_verdict(delivery: PromptDelivery) -> DeliveryVerdict:
    """The one reading of a send's answer."""
    if delivery.outcome is PromptOutcome.SENT:
        return DeliveryVerdict.SENT
    if delivery.outcome is PromptOutcome.UNCONFIRMED:
        return DeliveryVerdict.UNCONFIRMED
    reason = delivery.reason
    if reason is PromptReason.NOT_RUNNING:
        return DeliveryVerdict.NOT_RUNNING
    if reason in _WAITS:
        return DeliveryVerdict.WAIT
    if reason in _DIALOGS:
        return DeliveryVerdict.DIALOG
    if reason is PromptReason.COMPOSING:
        return DeliveryVerdict.COMPOSING
    if reason in _REFUSED:
        return DeliveryVerdict.REFUSED
    return DeliveryVerdict.UNRECOGNISED
