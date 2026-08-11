"""What the owner is told when an agent stops working, and how carefully it is said.

This is the only place in the project where the service speaks first. Every other screen
answers something the owner pressed, so a wrong word costs them a tap; here a wrong word
arrives on a phone at two in the morning and is acted on. The whole module is therefore about
the difference between what was *reported* and what was *guessed*, and about never letting the
second borrow the grammar of the first.

Two rules carry that, and both are structural rather than editorial:

**An inferred observation says so, in its own sentence.** `ActivityConfidence.INFERRED` covers
two very different guesses -- a sixty-second idle timer upstream, and a pane that stopped
changing here -- and neither is worth telling the owner as a fact. The hedge is appended by
the renderer, not left to whoever writes the next sentence.

**A quiet report never carries agent text.** Nothing said it. The classifier already sets
`detail=None` for `QUIET`, and this drops it again regardless, because the failure mode is
silent and specific: the last line of an idle screen rendered under the session's name reads
exactly like a parting statement the agent chose to make.
"""

from __future__ import annotations

from datetime import UTC

from remote_agents.adapters.telegram.presenters import (
    MAX_TELEGRAM_TEXT_UNITS,
    Button,
    RenderedMessage,
    _bounded_escaped,
    _utf16_units,
    _validate_callback,
    render_message,
)
from remote_agents.ports.agent_activity import (
    MAXIMUM_DETAIL_CHARACTERS,
    ActivityConfidence,
    ActivityKind,
    AgentActivity,
)

OPEN_SESSION_LABEL = "Open session"
"""The one thing a notification can do.

A notification is not a screen: it is sent apart from the live view and it is not the anchor,
so it may not carry navigation. One button, and it leads to the session the message is about.
"""

_HEDGE = "This is a guess, not something it reported."
"""Appended to every inferred observation, and to no reported one."""

_SENTENCES = {
    ActivityKind.COMPLETED: "The agent has finished its work.",
    ActivityKind.LIMIT_REACHED: "The agent stopped after reaching a usage limit.",
    ActivityKind.ENDED: "The session has ended.",
}

_WAITING = {
    ActivityConfidence.REPORTED: "The agent is waiting for an answer.",
    # Weaker on purpose: this reaches the owner from a sixty-second idle timer with recorded
    # false positives, so the sentence has to survive being wrong.
    ActivityConfidence.INFERRED: "The agent may be waiting for an answer.",
}

# The UTF-16 budget, the escape-then-fit routine and the callback shape are imported from
# `presenters` rather than copied, private names and all: an escaper and a budget that exist
# twice are two escapers and two budgets, and only one of them ever gets fixed.


def render_activity(activity: AgentActivity, *, display: str, open_session: str) -> RenderedMessage:
    """Render one observation as the message the owner receives about it.

    Pure, and deliberately ignorant of Telegram's transport: it is handed the session's
    display identity and an already-minted callback token because resolving either would mean
    this adapter reaching for a store, and the rendering is the part worth testing exhaustively.

    The bounding order is detail first, then the name. Either can be pathological -- a display
    identity carries an owner-supplied label -- and reserving the detail's slot before fitting
    the name means a long name truncates itself rather than silently deleting what the agent
    said.
    """
    _validate_callback(open_session)
    sentence = _sentence(activity)
    hedge = _HEDGE if activity.confidence is ActivityConfidence.INFERRED else ""

    detail = _bounded_escaped(_detail_of(activity) or "", MAXIMUM_DETAIL_CHARACTERS)
    tail = (f"\n{detail}" if detail else "") + (f"\n{hedge}" if hedge else "")

    skeleton = f"<b></b>\n{sentence}{tail}"
    name = _bounded_escaped(display, MAX_TELEGRAM_TEXT_UNITS - _utf16_units(skeleton))
    return render_message(
        f"<b>{name}</b>\n{sentence}{tail}",
        ((Button(OPEN_SESSION_LABEL, open_session),),),
    )


def _sentence(activity: AgentActivity) -> str:
    if activity.kind is ActivityKind.NEEDS_ANSWER:
        return _WAITING[activity.confidence]
    if activity.kind is ActivityKind.QUIET:
        # What was observed, never what it implies. The service saw a pane stop changing; it
        # did not see an agent finish, and the owner reading this on a phone will supply that
        # conclusion themselves if the sentence lets them.
        return f"No output since {_moment(activity)}."
    return _SENTENCES[activity.kind]


def _detail_of(activity: AgentActivity) -> str | None:
    """What the agent said, or nothing at all when nothing said it."""
    return None if activity.kind is ActivityKind.QUIET else activity.detail


def _moment(activity: AgentActivity) -> str:
    """The observation's instant, in one spelling.

    Normalized to UTC before it is formatted. A hook payload carries whatever offset the
    agent's host was in, and the pane watcher stamps UTC, so without this the same instant
    reaches the owner as two different clock times depending on which source noticed it.

    The minute is the whole precision on offer, and it is conservative in the direction that
    matters: `observed_at` is the moment the quiet threshold was *crossed*, so the true silence
    began `quiet_polls x poll_seconds` earlier. "No output since" this time is therefore true
    and understated, which is the right way for a heuristic to be wrong.
    """
    return activity.observed_at.astimezone(UTC).strftime("%H:%M UTC")
