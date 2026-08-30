"""Which of an agent's observations the owner is told about, and how they are bundled.

Policy, not delivery. Nothing here reads a clock, a bot, a session store or a socket: every
moment a rule reasons about arrives as an argument, which is what lets the eight-hour taper
proof be a loop over integers instead of a fake clock threaded through a Telegram double.
`ActivityNotifier` in `adapters/telegram/notifications.py` is the driver that asks these
questions and then does the sending; it kept the PTB verbs, the token minting (DEC-011) and the
wording.

**Clock-free is not the same as side-effect-free, and the difference is deliberate.**
`grouped_for_delivery` and its neighbours are pure functions. `record_sent`, `forget_expired`
and `enqueue` are not: they mutate a mapping or a sequence the *surface* owns and passes in.
That is the split this module is built on -- the rules moved, the state did not, on the same
reading DEC-026 already applied to the backlog -- and calling the whole module "pure" would
paper over the one thing a reader most needs to know about it.

**What this module may not do is say anything.** A function here returns a bundle, a tuple of
kinds, a selection -- a signal -- and never a sentence (DEC-043). The sentence is the surface's,
because the bot sizes its words for a chat message and a second frontend would size them
differently, and a shared renderer is how one surface's wording quietly becomes the other's.

For the same reason the line budget arrives as an argument rather than living here: `limit` is
keyword-only with no default on both functions that spend it, so this module cannot acquire an
opinion about a number the surface owns (DEC-034 accepted cost 4).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping, MutableSequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from remote_agents.ports.agent_activity import ActivityKind, AgentActivity


@dataclass(frozen=True, slots=True)
class SessionGroup:
    """One session's news for one delivery pass, in the order it should be read."""

    session_id: str
    activities: tuple[AgentActivity, ...]


def shown_in_message(group: SessionGroup, *, limit: int) -> tuple[AgentActivity, ...]:
    """Which of a group's observations one message actually spells out.

    The *newest* that fit, not the first. `grouped_for_delivery` orders a group oldest-first so
    it reads as a timeline, and taking a prefix therefore spelled out the stalest lines and
    folded the freshest into the counter -- a `needs_answer` arriving after five `completed`
    reports is the one line worth a thumb, and it was the one being hidden.

    **Public, and shared with the notifier, because the answer had two owners and they
    disagreed.** `activity_text` rendered the newest five while `_send` stamped the rate limit
    for every kind in the group, so an observation folded into "and N earlier" was recorded as
    told to the owner and then dropped, having been neither. For `NEEDS_ANSWER` -- the highest
    value signal this service has -- that is the agent waiting and nobody being told, with the
    stamp then suppressing the next report of it too. It is the same silence-by-self-suppression
    the window filter caused, reached through the line cap instead, which is the argument for
    one function rather than two call sites that happen to agree.

    `limit` is how many lines the surface will spell out. It is asked for rather than known
    here: the number is presentation, and the two frontends this backend serves would not
    answer it the same way.
    """
    return group.activities[-limit:]


def grouped_for_delivery(activities: Iterable[AgentActivity]) -> tuple[SessionGroup, ...]:
    """Fold a pass's observations into one bundle per session, saying each thing once.

    Pure and clock-free: it is handed everything the pass observed and reads nothing else, so
    the three rules below are exercised without a Telegram, a session store or a sleep.

    **Sessions come back in the order they were first heard from.** The queue behind this is
    FIFO, and that fairness is what keeps a burst of twenty sessions spread across passes
    rather than starving the unlucky ones -- a property that survives grouping only if grouping
    preserves it. Ordering by session id would hand the chat to whoever's identifier sorts
    early, every pass, for as long as the backlog lasts, and that identifier is not something
    the owner chose or can see. Ordering by time across sessions is the subtler mistake: it
    would decide a group's place from a stamp on one observation *inside* it, so the collapse
    below -- which changes which observation that is -- would be quietly re-deciding an order
    the queue had already settled.

    **Within a session, a kind collapses to its newest observation.** A `Stop` hook fires per
    turn rather than per task, so an agent working through one long instruction reports
    "finished" repeatedly and every report is true; the pane watch has the same shape, since
    `QUIET` carries no agent text at all and two quiet spells in one pass are indistinguishable
    here.

    **The kind is the identity, and the detail is not part of it (DEC-034).** This is the
    owner's correction to the shape this had, and the reason is what a notification is *for*:
    it tells them a session has stopped and wants them. Five "the agent has finished its work" lines
    carrying five different last replies do not tell them that five times over -- they tell
    them once, and then bury the sentence that matters under four stale copies of itself.
    Keyed on `(kind, detail)` those five were five distinct things and every one was rendered,
    which is how one session's message came to be a wall of the same sentence.

    What is given up is real and was weighed: the older text of a kind is dropped rather than
    shown, so a `completed` report whose reply the owner never read is gone. The session
    itself is the authoritative record of what an agent said (DEC-013), the message is the
    alert -- and the newest report of a kind is the one that describes the state the session
    is actually in now.

    **What survives is then ordered by `observed_at`.** After the collapse rather than before,
    and the order matters: a survivor carries the newest stamp of its duplicates, so a sentence
    first said at 14:00 and repeated at 14:20 belongs below whatever was said at 14:10. Sorted
    first and collapsed after, it would print above it -- a timeline running backwards inside a
    single message, which reads as the service having confused two sessions.

    Ties fall back to the collapse order, which is first appearance of each kind; the sort is
    stable and this relies on that. Two observations sharing an instant carry
    no fact about which came first, so what is worth guaranteeing is not the true order but a
    repeatable one: the same input has to produce the same bundle on a retry, because a message
    that reshuffles itself between one send and the next reads as fresh news.
    """
    collapsed: dict[str, dict[ActivityKind, AgentActivity]] = {}
    for activity in activities:
        seen = collapsed.setdefault(activity.session_id, {})
        key = activity.kind
        held = seen.get(key)
        if held is None or activity.observed_at > held.observed_at:
            # Assignment to an existing key keeps its position, so replacing a duplicate does
            # not move the survivor to the back of its group -- the collapse order stays first
            # appearance, which is what the stable sort below falls back on.
            seen[key] = activity
    return tuple(
        SessionGroup(
            session_id,
            tuple(sorted(observations.values(), key=lambda activity: activity.observed_at)),
        )
        for session_id, observations in collapsed.items()
    )


def merged(
    carried: tuple[AgentActivity, ...], arrived: tuple[AgentActivity, ...]
) -> tuple[AgentActivity, ...]:
    """Fold new observations into what a standing message already says.

    Delegated to `grouped_for_delivery` rather than re-implemented, because the two rules that
    matter here are its rules: a kind collapses to its newest observation, and what survives is
    ordered by `observed_at`. A second copy of them would drift, and the
    drift would be invisible -- a message whose lines are subtly out of order reads as the
    service having confused two sessions, which is exactly what that function's docstring is
    about.

    Both arguments belong to one session, so there is exactly one group to unpack.
    """
    groups = grouped_for_delivery((*carried, *arrived))
    return groups[0].activities if groups else ()


def for_update(
    session_id: str,
    carried: tuple[AgentActivity, ...],
    arrived: tuple[AgentActivity, ...],
    *,
    limit: int,
) -> SessionGroup:
    """Lay out a re-render so the lines nobody has seen yet are the ones it spells out.

    `shown_in_message` takes the newest `limit`, which is right for a message being sent for the
    first time and wrong for one being amended. An observation that arrives *older* than the
    ones the message already carries -- a `needs_answer` queued behind five newer `completed`
    reports -- would be folded into "and N earlier" on this pass, and on every pass after it,
    forever: the merge keeps putting it back in the same losing position. Under the old shape
    it escaped because the next pass sent it as a message of its own. There is no next message
    now, so the room has to be made here, and the drain deleted its record long ago.

    So arrivals claim slots first, the previously-shown fill what is left, and the result is
    laid out with those slots **last**, because the end of the tuple is where
    `shown_in_message` looks. Ordering within the shown set stays by `observed_at`, so the
    message still reads as a timeline.

    `limit` is the same budget `shown_in_message` spends, and it is passed in for the same
    reason: the two functions have to agree about it, and the way to guarantee that is for the
    caller to hold the one number rather than for each of them to hold a copy.
    """
    fresh = tuple(activity for activity in carried if activity in arrived)
    keep: list[AgentActivity] = list(fresh[-limit:])
    for activity in reversed(carried):
        if len(keep) >= limit:
            break
        if activity not in keep:
            keep.append(activity)
    shown = sorted(keep, key=lambda activity: activity.observed_at)
    buried = [activity for activity in carried if activity not in keep]
    return SessionGroup(session_id, (*buried, *shown))


def told(
    arrived: tuple[AgentActivity, ...], shown: tuple[AgentActivity, ...]
) -> tuple[ActivityKind, ...]:
    """The kinds this pass both heard and put in front of the owner.

    The rate limit's question is how often a session *reports* a kind, so a line the message
    is merely still displaying is not an answer to it. See the call site for why narrowing to
    this is not the narrowing `record_sent` warns against.
    """
    return tuple(activity.kind for activity in shown if activity in arrived)


def unheard(
    standing: tuple[AgentActivity, ...], shown: tuple[AgentActivity, ...]
) -> tuple[ActivityKind, ...]:
    """The kinds a re-render would put in front of the owner that its message does not carry.

    The question is not "has anything changed" -- a fresher `completed` carrying a different
    last reply changes the text and is still the same news -- but "is the owner being told
    something they have not been alerted to". Non-empty is what earns a message that arrives;
    empty is what a silent amendment is for.

    Keyed on the kind because the kind is what the sentence says, and the sentence is what the
    alert is: `completed` means the session stopped and wants them, and it means that exactly
    once until it stops meaning it. Comparing the observations themselves instead would make
    every repeat an alert again, which is the shape the owner asked to be rid of.
    """
    known = {activity.kind for activity in standing}
    heard_questions = {
        activity.detail for activity in standing if activity.kind is ActivityKind.NEEDS_ANSWER
    }
    return tuple(
        activity.kind
        for activity in shown
        if activity.kind not in known
        or (activity.kind is ActivityKind.NEEDS_ANSWER and activity.detail not in heard_questions)
    )


def unsaid(
    arrived: tuple[AgentActivity, ...], shown: tuple[AgentActivity, ...]
) -> tuple[AgentActivity, ...]:
    """Which of this pass's arrivals the message did not spell out, and therefore still owes.

    Measured against the arrivals rather than against everything the message accounts for,
    because those are two different sets once a message can be re-rendered. An observation
    that has dropped out of the newest few was *shown* on an earlier pass -- it has been
    told, and re-queueing it would print it a second time. One that arrived now and did not
    fit has been told to nobody, and the drain has already deleted its record, so letting it
    go loses agent output permanently.
    """
    return tuple(activity for activity in arrived if activity not in shown)


# The suppression window --------------------------------------------------------------------
#
# The rules move; the map does not. `ActivityNotifier` keeps holding the
# `dict[(session, kind), Sent]` for the same reason DEC-026 keeps the backlog in the adapter's
# memory -- residence is not policy -- and hands it to each function below. That is what makes
# these pure in the sense the sub-plan asked for: no object here survives between calls, and
# every moment they reason about arrives as an argument.

MAXIMUM_BACKOFF_DOUBLINGS = 5
"""How far the repeat window may double: 2 minutes to 64, and no further.

Capped rather than unbounded because an agent that has been waiting eight hours is still
waiting, and a window that keeps doubling eventually amounts to never telling the owner again.
"""


@dataclass(frozen=True, slots=True)
class Sent:
    """When a (session, kind) was last delivered, and how many consecutive times."""

    sent_at: datetime
    repeats: int


def window(repeats: int, *, rate_limit: timedelta) -> timedelta:
    """How long this news stays old, given how many times it has already been sent.

    A fixed window is the right answer for a burst and the wrong one for a **standing**
    condition. `Stop` fires per turn, so a busy agent repeats "finished" and one message
    per two minutes is a fair summary. But `needs_answer` repeats for as long as the owner
    does not answer -- which, at three in the morning, is all night -- and a fixed window
    turns that into a message every two minutes until they wake up. The pane-quiet path had
    carried the equivalent rule since it was written -- it reported once per spell and re-armed
    only on a change -- while the hook-sourced kinds had nothing, because the burst was the only
    case anyone had in mind. That path was retired on 2026-08-30 along with `ActivityKind.QUIET`;
    the rule it demonstrated is the one below, and it now has no other home.

    So the window doubles per consecutive repeat, capped: 2 minutes, 4, 8, 16, 32, then
    every 64 minutes for as long as it lasts. The first message arrives as fast as ever --
    this only ever makes the *second and later* copies rarer -- and the cap keeps the signal
    alive rather than muting it, because an agent that is still waiting is still news.

    `rate_limit` is the base window, asked for rather than held, on the same argument as the
    line budget: it is the surface's number, and a second frontend would answer it differently.
    """
    return rate_limit * (2 ** min(repeats, MAXIMUM_BACKOFF_DOUBLINGS))


def due(
    activity: AgentActivity,
    sent: Mapping[tuple[str, ActivityKind], Sent],
    moment: datetime,
    *,
    rate_limit: timedelta,
) -> bool:
    """Whether this one observation is news, given when its kind was last sent.

    Measured against the window that entry's *own* repeat count earns, not the base one --
    which is the whole of what the taper does, and the reason `moment` is a parameter rather
    than a clock read in here.
    """
    entry = sent.get((activity.session_id, activity.kind))
    return entry is None or moment - entry.sent_at >= window(entry.repeats, rate_limit=rate_limit)


def record_sent(
    sent: MutableMapping[tuple[str, ActivityKind], Sent],
    session_id: str,
    kinds: Iterable[ActivityKind],
    moment: datetime,
) -> None:
    """Stamp every kind this message carried, and let the rest of the session start over.

    A repeat count is a claim that *nothing has changed*. The moment a session reports a
    different kind, something has: an agent that finishes, is asked something, and finishes
    again is not repeating itself, and backing its second "finished" off to an hour would
    answer the wrong question. Only the counter resets -- the other kinds keep their stamps,
    so their base windows still collapse a genuine burst.

    **The kinds this message carried are exempt from that reset, and the exemption is the
    whole reason this takes a batch rather than a key.** The rule was written when a
    message carried exactly one kind, and applied per-kind to a grouped one it turns on
    itself: recording the second kind resets the first, which was recorded a moment earlier
    in the same send and is not evidence that anything changed.

    **`kinds` is what the message *carried*, which since `_send` stops filtering by window
    is everything the session was observed saying this pass** -- and that identity is what
    makes the exemption complete rather than partial. An earlier version passed only the
    kinds whose own window had elapsed, which left the dominant case open: a standing
    condition backed off to sixty-four minutes is *absent* from almost every message, so it
    was almost always the kind being reset, by its own suppression. Measured, that produced
    75 to 255 notifications overnight where the taper intends 12. A caller that ever
    narrows this argument again reopens exactly that, and nothing about the resulting
    messages would look wrong.

    So the reset applies to what the session did not say at all this pass, which is what "a
    different kind" always meant.

    Mutates the map it is given rather than returning a new one, because the map is the
    surface's and this is a rule being applied to it, not a second copy of it being made.
    """
    carried = set(kinds)
    for kind in carried:
        key = (session_id, kind)
        prior = sent.get(key)
        sent[key] = Sent(moment, 0 if prior is None else prior.repeats + 1)
    for other, entry in sent.items():
        if other[0] == session_id and other[1] not in carried and entry.repeats:
            sent[other] = Sent(entry.sent_at, 0)


RETENTION_WINDOWS = 2
"""How many of its own windows an entry is kept for after its suppression has lapsed.

Two, so a repeat arriving any time before the window has passed *again* is still recognised as
a repeat. One would mean the count died with the suppression it caused, and the backoff could
never reach its second step.
"""


def forget_expired(
    sent: MutableMapping[tuple[str, ActivityKind], Sent],
    moment: datetime,
    *,
    rate_limit: timedelta,
) -> None:
    """Keep the suppression map the size of what it is still suppressing.

    One entry per (session, kind) is small, but it is unbounded over the life of a service
    that launches sessions all day, and an entry older than its window suppresses nothing.

    Measured against **its own** window rather than the base one. Under a fixed horizon a
    backed-off entry -- the ones that matter, because they are the repeating ones -- was
    forgotten while it was still suppressing, which silently restored the every-two-minutes
    behaviour the backoff exists to remove, and did it only for standing conditions.

    And kept for `RETENTION_WINDOWS` times that, because the repeat count has to outlive
    the suppression it produced. Dropped the instant the window closed, the entry took the
    count with it, so the very next repeat looked like a first sighting and reset the
    backoff to two minutes -- a backoff that could never reach its second step, which is
    exactly as good as no backoff. The extra life is what makes a repeat recognisable *as*
    one; the entry is inert during it, since the window has already passed.

    **Under a floor, and the floor is the whole of the taper working at all.** Both terms
    above scale with the count they exist to preserve, so at zero repeats the horizon was
    four minutes -- and a kind observed less often than *that* always found its own entry
    already discarded, was re-created at zero, and could never reach the first doubling.
    The counter is what makes each wait longer, and it could not climb. `Stop` fires per
    turn and a turn routinely takes longer than four minutes, so this was the ordinary
    case: a lone `Stop` every five minutes produced 96 messages over eight hours against a
    taper intending twelve, and every notification in the pile was individually true.

    The bootstrapping problem is why a proportional horizon cannot fix itself: the entry
    must already have a high count to be kept long enough to earn a high count. So the
    floor is a fixed quantity that does not consult the count at all -- the widest window
    the backoff can ever reach. Anything reporting more often than that hourly cap now
    accumulates, which is every case the cap was designed for.

    It is a floor rather than a removal because the map is still unbounded over the life
    of a service launching sessions all day, and forgetting is what bounds it. A kind that
    genuinely stops reporting is still forgotten -- an hour or so later than before, one
    small entry per (session, kind) -- and that is the whole price.

    `moment` is a parameter rather than a clock read here, which is what lets the eight-hour
    run in `tests/unit/application/test_notification_policy.py` be a loop over integers
    instead of a fake clock threaded through a notifier.
    """
    floor = window(MAXIMUM_BACKOFF_DOUBLINGS, rate_limit=rate_limit)
    expired = [
        key
        for key, entry in sent.items()
        if moment - entry.sent_at
        >= max(window(entry.repeats, rate_limit=rate_limit) * RETENTION_WINDOWS, floor)
    ]
    for key in expired:
        del sent[key]


# The bounded backlog -----------------------------------------------------------------------
#
# The queue itself stays in the adapter's memory, with no table behind it and nothing spilled
# anywhere (DEC-026). What moves is the rule for what a full queue costs and whom.


REFUSALS_BEFORE_ABANDONING = 3
"""How many consecutive refusals a session's news survives before it is given up on.

Three, by the owner's decision of 2026-08-23 (DEC-049). Two would abandon on the second
attempt of a transient outage, which is the case the retry exists for; a larger number just
lengthens the outage a permanently-refused group inflicts on every other session behind it.
"""


def refused(refusals: MutableMapping[str, int], session_id: str, *, limit: int) -> bool:
    """Count a refusal against a session and answer whether its news is now abandoned.

    **The bug this closes is not that a refused group is retried -- it is that it blocks.**
    `deliver` stops the whole pass on a refusal and holds the group at the head of the queue,
    which is right for a 429 or an outage: nothing is lost and the order is kept. For a
    refusal that will never succeed -- a 400 on a malformed message -- the same group is
    retried and refused every pass, and because the refusal stops the pass, *no session in the
    chat is ever notified again*. One poisoned group is a chat-wide outage with no error
    anybody sees.

    Mutates the mapping it is handed rather than owning one (DEC-044): the rule is here, the
    count stays with the surface, and a caller that never asks again keeps no state at all.
    The entry is deleted on abandonment so the next refusal for that session starts from one,
    and `delivered` clears it so the count means *consecutive* refusals rather than lifetime
    ones -- a session that fails once a week is not a session anyone should give up on.
    """
    refusals[session_id] = refusals.get(session_id, 0) + 1
    if refusals[session_id] < limit:
        return False
    del refusals[session_id]
    return True


def delivered(refusals: MutableMapping[str, int], session_id: str) -> None:
    """Forget a session's refusals, because the streak this counts is a consecutive one."""
    refusals.pop(session_id, None)


def forget_absent(refusals: MutableMapping[str, int], present: Iterable[str]) -> None:
    """Drop counts for sessions no longer queued, so the map cannot grow for the process's life.

    A session can leave the queue without ever succeeding or being abandoned -- the 200-cap
    evicts it, or `retire_finished` retires it -- and a count nobody will ever clear is the
    unbounded map this module already deleted once.
    """
    for session_id in set(refusals) - set(present):
        del refusals[session_id]


def enqueue(
    pending: MutableSequence[AgentActivity],
    activities: Iterable[AgentActivity],
    *,
    maximum: int,
) -> tuple[tuple[str, int], ...]:
    """Take a pass's observations, dropping the loudest session's oldest when full.

    **Not the queue's oldest, which is what this did.** Delivery is per session and so is
    fairness -- `grouped_for_delivery` orders by first appearance precisely so a burst
    cannot starve a quiet session -- but retention was global and per observation, so one
    chatty session could own all hundred slots and evict every other session's news from
    the head. Simulated: five sessions each reporting once, against one session emitting
    twenty-five distinct records a pass, ended with the queue holding a hundred observations
    from the loud session and nothing from the other five. Their reports were destroyed
    permanently -- the drain deletes a record before returning it, so an evicted observation
    has no second chance anywhere in the system.

    Evicting from the session with the most queued observations makes the cap cost the
    session that filled it. Its *oldest* goes, because within one session the newest news
    is the news worth keeping.

    **Reports rather than says (DEC-043).** Each eviction comes back as
    `(session_id, how many that session was holding)` and nothing here writes a sentence: the
    operator-facing warning is sized for a journal line, which is the surface's business, and a
    frontend with no journal would not write one at all.

    `maximum` is asked for on the same argument as every other bound in this module -- it is
    the surface's number, written down beside the drain's own cap that it was sized against.
    """
    evicted: list[tuple[str, int]] = []
    for activity in activities:
        if len(pending) >= maximum:
            evicted.append(_evict_loudest(pending))
        pending.append(activity)
    return tuple(evicted)


def _evict_loudest(pending: MutableSequence[AgentActivity]) -> tuple[str, int]:
    """Drop one observation from whichever session is using the most of the queue.

    Deliberately without an empty-queue guard, because the original had none and this is a
    relocation. A guard here would be unreachable at the production cap and would, at
    `maximum=0`, turn a raise into a silent no-op -- swapping a loud failure for a quiet one
    in the one case nobody has thought about. Reached only from `enqueue`, and only when the
    queue is already at its bound.
    """
    counts: dict[str, int] = {}
    for held in pending:
        counts[held.session_id] = counts.get(held.session_id, 0) + 1
    loudest = max(counts, key=lambda session_id: counts[session_id])
    for index, held in enumerate(pending):
        if held.session_id == loudest:
            del pending[index]
            break
    return loudest, counts[loudest]
