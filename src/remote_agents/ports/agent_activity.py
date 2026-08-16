"""What an agent was last observed doing, in the vocabulary the owner is told it in."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from remote_agents.ports.terminal_text import encodable_text

MAXIMUM_DETAIL_CHARACTERS = 240
"""How much of what an agent said a notification will carry.

It lives here because both ends of the spool have to agree on it: the hook bounds what it
writes, and the drain bounds again what it reads, since a different process wrote the file in
between. Two copies of this number would be two budgets, and nothing would notice them
drifting apart -- each side's tests only ever exercise its own.
"""


def bounded_detail_line(value: object) -> str | None:
    """Reduce free agent text to the one bounded line a notification can show.

    Not a formatting nicety: this text arrives from whatever the agent last said, so it may
    carry newlines that would break a message into pieces, runs of whitespace from a rendered
    table, or a whole essay. One line, bounded, or nothing.
    """
    if not isinstance(value, str):
        return None
    # Surrogates go before anything else looks at this. A lone surrogate is a legal `str` and
    # an illegal encode, so it survives `split()`, survives the spool's `json.dumps` as
    # `\udXXX`, and detonates at the far end where the text is measured for a message budget.
    # Dropping it at the boundary where free agent text is first reduced is cheaper than every
    # later consumer being total -- and the presentation layer is made total anyway, through
    # this same function, for the inputs that never come through here at all.
    normalized = " ".join(encodable_text(value).split())
    return normalized[:MAXIMUM_DETAIL_CHARACTERS] if normalized else None


class ActivityKind(Enum):
    """The only things this service claims about an agent that has stopped working.

    Deliberately fewer than the events upstream emits. A hook carries a dozen error types and
    notification types; the owner is being told one of five things, and an event that does not
    answer "why did it stop" is dropped rather than mapped to the nearest neighbour.

    Answering that question is necessary and not sufficient. Every message built from these
    arrives unprompted, on a phone, so the second bar is that the owner has something to *do*
    about it. An `ended` kind cleared the first and failed the second: it came from `SessionEnd`,
    which fires on the stop the owner has just pressed, so it spent its life confirming their own
    action back to them. It is retired rather than reworded, because there is no wording that
    makes news out of something that is not news.

    `LIMIT_REACHED` and `OUTPUT_LIMIT` are separate because the owner's next move differs. A
    rate limit is waited out or paid around and the work is untouched; a response that hit its
    output ceiling is simply continued. Folded together -- as they were, under one "usage
    limit" sentence -- the message named the more alarming of the two for an event that is
    routine, which is the same over-claiming the confidence split exists to prevent.
    """

    COMPLETED = "completed"
    LIMIT_REACHED = "limit_reached"
    OUTPUT_LIMIT = "output_limit"
    NEEDS_ANSWER = "needs_answer"
    QUIET = "quiet"


class ActivityConfidence(Enum):
    """Whether the agent said this, or something guessed it from the outside.

    It exists so presentation can weaken its wording rather than flatten the difference. The one
    guess left is pane quiet, and it is a heuristic by construction: this service watched a
    terminal stop changing and inferred an agent behind it. Nothing can check that inference,
    either -- the profiles watched this way are exactly the ones with no hook system, so pane
    quiet is not a second opinion about a session, it is the only opinion there is. Worth
    telling the owner, and not worth telling them as a fact.

    Two members for one guess is deliberate. This records a property of the *observation* --
    whether anything actually said it -- rather than a count of today's sources, so a signal
    arriving from somewhere new is classified before anyone chooses its words. That ordering is
    what kept Claude's sixty-second idle notification, which `application/activity.py` no longer
    maps at all, from ever reaching the owner in the grammar of something the agent had said.
    Naming it here rather than leaving it as "a retired upstream signal" is the point of the
    example: the member that outlived it is the one a reader is entitled to ask about.
    """

    REPORTED = "reported"
    INFERRED = "inferred"


@dataclass(frozen=True, slots=True)
class AgentActivity:
    """One observation about one session, bounded and ready to render."""

    session_id: str
    kind: ActivityKind
    detail: str | None
    observed_at: datetime
    confidence: ActivityConfidence = ActivityConfidence.REPORTED


HOOK_SOURCED_PROFILES = frozenset({"claude", "claude-remote"})
"""The profiles whose own hooks report what they are doing.

Everything else -- codex, opencode, cursor-agent -- has no hook system, so the only evidence
available about it is its pane. The distinction lives here rather than in `domain/profiles.py`
because it is a fact about where *activity* comes from, not about how a profile launches, and
a profile gaining a hook system upstream changes this line and nothing else.

It is a subtraction, not an optimization: a session that reports its own Stop and is *also*
watched for pane quiet tells the owner the same thing twice, in two different wordings, one of
them a guess.
"""
