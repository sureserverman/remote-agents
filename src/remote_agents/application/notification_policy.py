"""Which of an agent's observations the owner is told about, and how they are bundled.

Policy, not delivery. Nothing here reads a clock, a bot, a session store or a socket: every
moment a rule reasons about arrives as an argument. That was what let the eight-hour taper
proof be a loop over integers instead of a fake clock threaded through a Telegram double; the
taper is gone (DEC-048) and the property is kept, because it is what makes every rule here
testable without a clock at all.
`ActivityNotifier` in `adapters/telegram/notifications.py` is the driver that asks these
questions and then does the sending; it kept the PTB verbs, the token minting (DEC-011) and the
wording.

**Clock-free is not the same as side-effect-free, and the difference is deliberate.**
`grouped_for_delivery` and its neighbours are pure functions. `enqueue` and `refused` are not:
they mutate a mapping or a sequence the *surface* owns and passes in.
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

from collections.abc import Iterable, MutableMapping, MutableSequence
from dataclasses import dataclass

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
    "finished" repeatedly and every report is true. The Codex title edge has the same shape for
    the same reason: it carries no agent text, so two observations of it in one pass are
    indistinguishable here and the newest is the whole of what the older one said.

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

    A line the message is merely still displaying is not something the session reported this
    pass, and the two are easy to conflate because the rendered message shows both.

    This used to cite `record_sent`'s warning about narrowing. That function was the taper's
    and went with it (DEC-048); the distinction it warned about is real without it, so the
    argument is stated here rather than pointed at.
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
