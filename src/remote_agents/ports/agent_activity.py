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
    # Then the characters that survive an encoder and still lie to a reader. `str.isprintable()`
    # is False for every `Cc` and `Cf` code point, which is the same filter -- and the same
    # argument -- `terminal_text.probe_version_line` already applies to a version banner: it
    # covers `U+202E` RIGHT-TO-LEFT OVERRIDE, `U+200B` ZERO WIDTH SPACE and `U+00AD` SOFT
    # HYPHEN, "the ones a reader would not think to check for".
    #
    # It belongs here rather than at either renderer because this text is an *agent's*, and an
    # agent writes whatever it likes. Escaping is not the same guard and does not substitute:
    # `html.escape` makes markup inert and leaves a bidi override untouched, so a notification
    # can arrive correctly escaped and still display its words in an order the agent chose. It
    # arrives unprompted on a phone at the moment the owner is deciding whether to act on it,
    # which is the worst possible place to render reordered text.
    #
    # The whitespace collapse above runs first on purpose: newline and tab are `Cc`, and they
    # are meant to become spaces rather than to vanish. Space itself is printable, so it
    # survives this. Found by the Stage 2 gate's second review, which noticed the project had
    # already solved this class one module over and not reused it.
    printable = "".join(character for character in normalized if character.isprintable())
    if not printable:
        return None
    if len(printable) <= MAXIMUM_DETAIL_CHARACTERS:
        return printable
    # Marked, because an unmarked cut is indistinguishable from the agent having stopped there.
    # A hard slice ended mid-word with nothing to say it had been cut -- "...used for validation,
    # formatt" reads as a sentence the agent left unfinished, which is a different and more
    # alarming thing than a message this service shortened. `feed._elide` already appends one for
    # the pane's own narrower width; this is the same courtesy at the budget both ends of the
    # spool agree on. The ellipsis is inside the bound, not added to it, so every caller's
    # arithmetic is unchanged.
    return printable[: MAXIMUM_DETAIL_CHARACTERS - 1] + "…"


class ActivityKind(Enum):
    """The only things this service claims about an agent that has stopped working.

    Deliberately fewer than the events upstream emits. A hook carries a dozen error types and
    notification types; the owner is being told one of four things, and an event that does not
    answer "why did it stop" is dropped rather than mapped to the nearest neighbour.

    Answering that question is necessary and not sufficient. Every message built from these
    arrives unprompted, on a phone, so the second bar is that the owner has something to *do*
    about it. An `ended` kind cleared the first and failed the second: it came from `SessionEnd`,
    which fires on the stop the owner has just pressed, so it spent its life confirming their own
    action back to them. It is retired rather than reworded, because there is no wording that
    makes news out of something that is not news.

    A `quiet` kind was retired on 2026-08-30 for failing the second bar in the other direction.
    It reported that a pane had stopped changing, which is true of an agent that has finished, an
    agent that is thinking, and an agent nobody has typed at since Tuesday. Nothing said it and
    nothing could check it -- the profiles watched that way were exactly the ones with no hook
    system -- so it was the only kind whose sentence had to be hedged, and the hedge was the
    honest reading of a signal that told the owner nothing they could act on. Retired here, at
    the vocabulary, rather than suppressed at a renderer: a kind that cannot be constructed
    cannot be stored, grouped, rate-limited or delivered by mistake somewhere further down.

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


class ActivityConfidence(Enum):
    """Whether the agent said this, or something guessed it from the outside.

    It exists so presentation can weaken its wording rather than flatten the difference. The one
    guess left is Codex's native approval wait, read off a pane *title* rather than pane content
    (DEC-063): the marker says a prompt is open, and this service infers an agent behind it
    waiting. Nothing else can check that -- the escalation bypasses Codex's own
    `PermissionRequest` hook, which is why the inference exists at all. Worth telling the owner,
    and not worth telling them as a fact.

    Two members for one guess is deliberate. This records a property of the *observation* --
    whether anything actually said it -- rather than a count of today's sources, so a signal
    arriving from somewhere new is classified before anyone chooses its words. That ordering has
    now outlived two of its own examples: Claude's sixty-second idle notification, which
    `application/activity.py` never maps, and pane quiet, retired on 2026-08-30 with its digest
    watch. Both were classified before anyone chose their words, and neither could borrow the
    grammar of something an agent had said while waiting to be retired -- which is what the
    member is for.
    """

    REPORTED = "reported"
    INFERRED = "inferred"


class ActivitySource(Enum):
    """How a provider contributes activity observations.

    This is deliberately provider capability rather than installer state. A hook-exclusive
    source has a stable, already-qualified hook path and is watched no other way. A hybrid
    source is Codex alone: it reports through hooks *and* carries a native approval escalation
    that bypasses them, which the pane-title edge infers (DEC-063).

    `UNOBSERVED` replaced `QUIET_ONLY` on 2026-08-30, when the pane-digest watch was retired
    with the `quiet` kind. The old member described profiles this service watched by hashing
    their pane; there is no such watch now, so the honest name for `opencode`, `cursor-agent`
    and anything else uncurated is that nothing observes them. It is a member rather than an
    absence because the watcher has to be able to *skip* them by name: a profile that reaches
    the polling loop costs a tmux capture per pass for an observation that can never be made.
    """

    HOOK_EXCLUSIVE = "hook_exclusive"
    HYBRID = "hybrid"
    UNOBSERVED = "unobserved"


_HOOK_EXCLUSIVE_PROFILES = frozenset({"claude", "claude-remote"})
_HYBRID_PROFILES = frozenset({"codex"})
_REPORTED_KINDS_BY_PROFILE: dict[str, frozenset[ActivityKind]] = {
    "claude": frozenset(
        {
            ActivityKind.COMPLETED,
            ActivityKind.LIMIT_REACHED,
            ActivityKind.OUTPUT_LIMIT,
            ActivityKind.NEEDS_ANSWER,
        }
    ),
    "claude-remote": frozenset(
        {
            ActivityKind.COMPLETED,
            ActivityKind.LIMIT_REACHED,
            ActivityKind.OUTPUT_LIMIT,
            ActivityKind.NEEDS_ANSWER,
        }
    ),
    # Codex exposes Stop and PermissionRequest hooks. It does not expose a StopFailure
    # equivalent, so limit/output kinds stay absent rather than being guessed from pane text.
    "codex": frozenset({ActivityKind.COMPLETED, ActivityKind.NEEDS_ANSWER}),
}


def activity_source_for(profile_id: str) -> ActivitySource:
    """Return the evidence policy for one curated provider profile."""
    if profile_id in _HOOK_EXCLUSIVE_PROFILES:
        return ActivitySource.HOOK_EXCLUSIVE
    if profile_id in _HYBRID_PROFILES:
        return ActivitySource.HYBRID
    return ActivitySource.UNOBSERVED


def reported_activity_kinds_for(profile_id: str) -> frozenset[ActivityKind]:
    """Return the activity kinds a provider can report without inference.

    This capability boundary deliberately says nothing about spool payload parsing. The parser
    is shared by the existing Claude hooks; the provider installer decides which upstream event
    names can reach it. Keeping the two facts separate prevents Codex from inheriting a Claude
    StopFailure claim merely because both use the same private spool.
    """
    return _REPORTED_KINDS_BY_PROFILE.get(profile_id, frozenset())


@dataclass(frozen=True, slots=True)
class AgentActivity:
    """One observation about one session, bounded and ready to render."""

    session_id: str
    kind: ActivityKind
    detail: str | None
    observed_at: datetime
    confidence: ActivityConfidence = ActivityConfidence.REPORTED


HOOK_SOURCED_PROFILES = _HOOK_EXCLUSIVE_PROFILES
"""The profiles whose own hooks report what they are doing.

Codex is intentionally not in this compatibility constant yet: it is a hybrid source, whose
hook may be absent, disabled, or awaiting the owner's trust review. `activity_source_for` is the
complete provider contract; the application layer uses its hybrid branch to watch Codex panes
for the native approval marker its own hook never sends.
"""
