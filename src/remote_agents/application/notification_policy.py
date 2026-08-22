"""Which of an agent's observations the owner is told about, and how they are bundled.

Policy, not delivery. Everything here is pure and clock-free: it is handed what a pass observed
and reads nothing else -- no bot, no session store, no `datetime.now`, no sleep -- so the rules
below are exercised directly rather than through a fake Telegram. `ActivityNotifier` in
`adapters/telegram/notifications.py` is the driver that asks these questions and then does the
sending; it kept the PTB verbs, the token minting (DEC-011) and the wording.

**What this module may not do is say anything.** A function here returns a bundle, a tuple of
kinds, a selection -- a signal -- and never a sentence (DEC-043). The sentence is the surface's,
because the bot sizes its words for a chat message and a second frontend would size them
differently, and a shared renderer is how one surface's wording quietly becomes the other's.

For the same reason the line budget arrives as an argument rather than living here: `limit` is
keyword-only with no default on both functions that spend it, so this module cannot acquire an
opinion about a number the surface owns (DEC-034 accepted cost 4).
"""

from __future__ import annotations

from collections.abc import Iterable
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
    return tuple(activity.kind for activity in shown if activity.kind not in known)


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
