"""What the owner is told when an agent stops working, and how carefully it is said.

This is the only place in the project where the service speaks first. Every other screen
answers something the owner pressed, so a wrong word costs them a tap; here a wrong word
arrives on a phone at two in the morning and is acted on. The whole module is therefore about
the difference between what was *reported* and what was *guessed*, and about never letting the
second borrow the grammar of the first.

Two rules carry that, and both are structural rather than editorial:

**An inferred observation says so, in its own sentence.** `ActivityConfidence.INFERRED` covers
one guess -- a pane that stopped changing here -- and it is not worth telling the owner as a
fact. The hedge is appended by the renderer, not left to whoever writes the next sentence, and
that stays structural now that it fires for a single kind: it was written when a second guess
existed, that guess was retired rather than hedged better, and the rule is what stops the next
one arriving in the grammar of something the agent actually said.

**A quiet report never carries agent text.** Nothing said it. The classifier already sets
`detail=None` for `QUIET`, and this drops it again regardless, because the failure mode is
silent and specific: the last line of an idle screen rendered under the session's name reads
exactly like a parting statement the agent chose to make.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from remote_agents.adapters.telegram.live_view import LiveView
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
from remote_agents.ports.callback_state import CallbackStatePort

_LOG = logging.getLogger(__name__)

OPEN_SESSION_LABEL = "Open session"
"""The one thing a notification can do.

A notification is not a screen: it is sent apart from the live view and it is not the anchor,
so it may not carry navigation. One button, and it leads to the session the message is about.
"""

NOTIFIED_DETAIL_ACTION = "session.detail.notified"
"""`session.detail`, reached from a notification rather than from a screen.

It exists so the press can be told apart at the boundary, which needs it for exactly one
decision: a notification is sent *apart* from the live view, so pressing it must not let it be
adopted as the live view. `service` normalizes it back to `session.detail` the moment that
decision is taken -- it is a fact about which message the button was on, not about the action.

Defined here rather than in `service` because the dependency runs this way: `service` imports
this module to build the notifier, so the constant has to live on this side of it.
"""

_HEDGE = "This is a guess, not something it reported."
"""Appended to every inferred observation, and to no reported one."""

_SENTENCES = {
    ActivityKind.COMPLETED: "The agent has finished its work.",
    ActivityKind.LIMIT_REACHED: "The agent stopped after reaching a usage limit.",
    ActivityKind.OUTPUT_LIMIT: "The agent stopped at its output length limit for one reply.",
}

_WAITING = "The agent is waiting for an answer."
"""The one sentence `NEEDS_ANSWER` has, now that every source of it is the agent's own report.

There were two, chosen by confidence, and the weaker of them -- "may be waiting" -- existed for
an upstream idle timer that is no longer mapped at all. Stating this plainly does not loosen
this module's first rule: what reaches here is an agent asking permission or an agent saying it
needs input, and in both the agent is the one making the claim.

"An agent asking permission" is spelled the long way round on purpose: upstream calls that case
`permission_` followed by the word for a question put to a terminal, and
`check_telegram_actions.py` rejects that bare substring anywhere in this package, because a
Telegram adapter that can put a question into a pane is the surface that audit exists to keep
closed. The check cannot tell prose from code, and it is right not to try: a term that has to
be spelled around in a comment is a term nobody adds to a call site by accident.
"""

# The UTF-16 budget, the escape-then-fit routine and the callback shape are imported from
# `presenters` rather than copied, private names and all: an escaper and a budget that exist
# twice are two escapers and two budgets, and only one of them ever gets fixed.


_MAXIMUM_LINES_PER_MESSAGE = 5
"""How many of a session's observations one message will spell out.

A backstop, not the mechanism: the rate limit collapses a burst of one kind and
`grouped_for_delivery` folds exact repeats, so a group this large has genuinely said this many
different things. Not necessarily *in one pass*, which an earlier version of this note claimed --
a group deferred past the per-pass ceiling, or held through a refusal, accumulates across as many
passes as it waits, and that is the case most likely to reach the cap at all.

What this may not do is drop the rest silently -- a message that looks like a complete account of
a session and is not is worse than one that says how much it left out, which is why the count
below is part of the rule rather than a nicety.
"""

_MAXIMUM_DETAIL_UNITS = MAXIMUM_DETAIL_CHARACTERS
"""One detail's ceiling in UTF-16 units, which is not the same quantity as its character cap.

Numerically the application layer's `MAXIMUM_DETAIL_CHARACTERS`, and named apart from it because
they are different measurements that happen to agree: that one truncates raw agent text by
character count before it is escaped, this one bounds an *escaped* line against Telegram's
budget. Reusing the import directly type-checked and read fine, and meant that raising the raw
cap for some unrelated reason would silently widen what one line may spend here, with nobody
editing this module.
"""

_BULLET = "• "
"""Worn only when there is more than one line to tell apart. See `activity_text`."""

_RESERVED_NAME_UNITS = 48
"""The slice of the budget the observations may not spend, so the session can still be named.

An untitled message is not a cheaper message, it is an unusable one: the owner reads these on a
phone, several sessions deep, and a wall of sentences with no idea which agent produced them
cannot be acted on at all.
"""


def shown_in_message(group: SessionGroup) -> tuple[AgentActivity, ...]:
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
    """
    return group.activities[-_MAXIMUM_LINES_PER_MESSAGE:]


def activity_text(group: SessionGroup, *, display: str) -> str:
    """One session's news as the whole message the owner receives about it.

    Split out from `render_activity` because the delivery order is send-then-mint: the
    notification's token is bound to the message the send answers with, so the text has to
    exist before the token does. Nothing about the wording depends on the button, which is
    what makes that order available at all.

    **A lone observation reads exactly as it always did**, sentence then detail on its own
    line, and only a group of two or more takes bullets with the detail folded onto the
    sentence's line. Two shapes is a real cost and it buys the two things that matter. Almost
    every notification carries one observation, so a bullet in front of a single sentence would
    be clutter added to the common case for the sake of the rare one. And in a group the fold
    is not cosmetic: with each detail on its own line, three observations produce six lines
    with nothing saying which text belongs to which sentence, and the owner attributes the
    agent's words to the wrong event.

    **The budget is spent outward from the observations.** The old order -- bound the detail,
    then fit the name into what was left -- was sufficient for exactly one observation and is
    not any more: the application layer caps each detail at `MAXIMUM_DETAIL_CHARACTERS`, but
    escaping can quintuple a character (`&` becomes `&amp;`), so five capped details can reach
    six thousand UTF-16 units against a ceiling of 4096, with no single one of them at fault.
    Telegram refuses the send outright, so the failure mode is a notification that never
    arrives. So the details share what is left after the sentences, the hedge and a reserved
    slot for the name, and the name is fitted last into whatever remains -- a long name
    truncates itself rather than deleting what an agent said, which is the right way round,
    since the name carries an owner-supplied label and the observations are what the message
    exists to deliver.

    **One hedge covers the group.** Repeated per line it would read as emphasis -- as though
    the service were less sure this time -- when it is saying the same structural thing about
    the same kind.
    """
    shown = shown_in_message(group)
    hidden = len(group.activities) - len(shown)
    bulleted = len(shown) > 1

    sentences = [_sentence(activity) for activity in shown]
    details = [_detail_of(activity) for activity in shown]
    hedged = any(activity.confidence is ActivityConfidence.INFERRED for activity in shown)

    trailers = ([f"and {hidden} earlier."] if hidden else []) + ([_HEDGE] if hedged else [])
    # Measured with the details empty, because they are the only part with a budget to
    # negotiate; everything else in the message is ours and fixed.
    spent = _utf16_units("<b></b>\n" + "\n".join(_lines(sentences, [None] * len(shown), bulleted)))
    spent += _utf16_units("\n" + "\n".join(trailers)) if trailers else 0
    share = (MAX_TELEGRAM_TEXT_UNITS - _RESERVED_NAME_UNITS - spent) // max(
        1, sum(1 for detail in details if detail)
    )

    bounded = [
        _bounded_escaped(detail, min(_MAXIMUM_DETAIL_UNITS, share)) if detail else None
        for detail in details
    ]
    body = "\n".join(_lines(sentences, bounded, bulleted) + trailers)
    name = _bounded_escaped(display, MAX_TELEGRAM_TEXT_UNITS - _utf16_units(f"<b></b>\n{body}"))
    return f"<b>{name}</b>\n{body}"


def _lines(sentences: list[str], details: list[str | None], bulleted: bool) -> list[str]:
    """One line per observation when they must be told apart, two when there is only one."""
    if not bulleted:
        return [sentences[0]] + ([details[0]] if details and details[0] else [])
    return [
        f"{_BULLET}{sentence}" + (f" — {detail}" if detail else "")
        for sentence, detail in zip(sentences, details, strict=True)
    ]


def render_activity(group: SessionGroup, *, display: str, open_session: str) -> RenderedMessage:
    """Render one session's group as the whole message the owner receives about it.

    Pure, and deliberately ignorant of Telegram's transport: it is handed the session's
    display identity and an already-minted callback token because resolving either would mean
    this renderer reaching for a store, and the rendering is the part worth testing
    exhaustively.
    """
    _validate_callback(open_session)
    return render_message(
        activity_text(group, display=display),
        ((Button(OPEN_SESSION_LABEL, open_session),),),
    )


def _sentence(activity: AgentActivity) -> str:
    if activity.kind is ActivityKind.NEEDS_ANSWER:
        return _WAITING
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


_RATE_LIMIT_SECONDS = 120.0
"""How long one session's one kind of news stays old.

A `Stop` hook fires per turn, not per task, so an agent working through a long instruction
reports "finished" repeatedly and each report is true. The owner does not need five of them.
Scoped to (session, kind) rather than to the chat: an agent that finishes and then needs an
answer has said two different things, and collapsing those would lose the one worth acting on.
"""

_MAXIMUM_BACKOFF_DOUBLINGS = 5
"""How far the repeat window may double: 2 minutes to 64, and no further.

Capped rather than unbounded because an agent that has been waiting eight hours is still
waiting, and a window that keeps doubling eventually amounts to never telling the owner again.
"""


@dataclass(frozen=True, slots=True)
class _Sent:
    """When one (session, kind) last went out, and how many times running."""

    sent_at: datetime
    repeats: int


@dataclass(frozen=True, slots=True)
class _Standing:
    """The one message a session owns in the chat, and what it currently says.

    A session gets a message, not a stream of them. New news re-renders this one rather than
    arriving beside it, so a session that reports for eight hours occupies one slot in the
    chat instead of ninety-six -- which is the whole point, and is what per-pass grouping
    could never reach: two turns thirty minutes apart are in different passes by definition.

    `activities` is what the message spells out, kept so the re-render can carry the whole
    story rather than only the newest arrival. Without it an edit would replace "finished,
    then asked a question" with "asked a question", silently deleting agent output that the
    drain has already removed from disk.

    `token` is the callback the message's button carries. A replacement moves it onto the new
    message with `rebind` rather than minting a fresh one, so a session reporting all day adds
    one row to a size-bounded store instead of one per report -- the same reason
    `LiveView.move_to_bottom` rebinds rather than re-mints.
    """

    message_id: int
    activities: tuple[AgentActivity, ...]
    token: str


_RETENTION_WINDOWS = 2
"""How many of its own windows an entry is kept for after its suppression has lapsed.

Two, so a repeat arriving any time before the window has passed *again* is still recognised as
a repeat. One would mean the count died with the suppression it caused, and the backoff could
never reach its second step.
"""

_MAXIMUM_SENDS_PER_PASS = 10
"""The ceiling the per-(session, kind) limit cannot provide, because it is per key.

That limit collapses one session repeating itself; it says nothing about many sessions
speaking at once. Twenty managed sessions stopping together are twenty *distinct* keys, none
suppressing any other, and each notification costs two Bot API calls -- past Telegram's
per-chat rate, at which point the 429s land in the retry queue and the backlog grows against
its own cap.

Ten per pass against a thirty-second poll averages a third of a message per second. *Averages*
is the honest word and was not the word here before: the sends are not paced within a pass, so
ten of them go out back to back -- ten `sendMessage`, ten `editMessageReplyMarkup`, and the live
view's delete-and-resend -- and no `AIORateLimiter` is installed on the application. The bound is
therefore a burst ceiling that happens to be under the *average* rate, not a rate limiter.

It degrades safely, which is why the number stands rather than the pacing being built: a 429
raises out of the send, `deliver` holds that group and stops the pass, and the rest waits. So the
failure mode of the optimistic reading is a slower pass, not a lost notification. Nothing is
dropped: the remainder stays queued and the next pass takes it, so a genuine burst arrives spread
out rather than refused. This is the only bound here that is about the *chat* rather than about
one session's news.
"""

_MAXIMUM_PENDING = 200
"""How many undelivered notifications are worth holding while Telegram is unreachable.

The drain has already deleted the spool files by the time a send is refused, so this queue is
the only remaining copy -- which is the argument for holding some, and the reason it cannot be
unbounded. An outage long enough to overflow this has produced news too stale to send anyway,
so the oldest is dropped and said out loud.

**Two hundred, and the number is `application.activity.MAXIMUM_DRAIN`'s rather than a taste.**
At a hundred it was half of what one drain may hand over in a single call, so a service that had
been down -- exactly the case `MAXIMUM_DRAIN`'s own docstring contemplates -- evicted a hundred
records inside `_enqueue` before a single send was attempted, and the drain had already unlinked
them from disk. `drain_activity` goes to real trouble to take the *oldest* records so its bound
cannot truncate whole sessions; the notifier then discarded exactly that oldest half. The two
bounds were set against each other across the seam. It is not imported, because an adapter
importing the application layer would invert the dependency this project is arranged around
(DEC-001); it is written down here with the reason, and the drain's constant is the one to look
at if either moves.

That there is nothing *behind* this cap is DEC-026 rather than an omission. A durable queue was
weighed and declined: it buys back a convenience at the price of a schema migration and a second
spool that must then be drained, bounded and reasoned about forever. So this number is the only
bound there is, and what it turns away is dropped rather than spilled anywhere.
"""


@dataclass(frozen=True, slots=True)
class SessionGroup:
    """One session's news for one delivery pass, in the order it should be read."""

    session_id: str
    activities: tuple[AgentActivity, ...]


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

    **Within a session, identical `(kind, detail)` observations collapse to the newest.** A
    `Stop` hook fires per turn rather than per task, so an agent working through one long
    instruction reports "finished" repeatedly and every report is true; the pane watch has the
    same shape, since `QUIET` carries no agent text at all and two quiet spells in one pass are
    indistinguishable here. What does not collapse is two `completed` observations carrying
    *different* text: those are two different things the agent said, and folding them on the
    kind alone would delete one of them silently. The pair is the identity, never the kind.

    **What survives is then ordered by `observed_at`.** After the collapse rather than before,
    and the order matters: a survivor carries the newest stamp of its duplicates, so a sentence
    first said at 14:00 and repeated at 14:20 belongs below whatever was said at 14:10. Sorted
    first and collapsed after, it would print above it -- a timeline running backwards inside a
    single message, which reads as the service having confused two sessions.

    Ties fall back to the collapse order, which is first appearance of each `(kind, detail)`
    pair; the sort is stable and this relies on that. Two observations sharing an instant carry
    no fact about which came first, so what is worth guaranteeing is not the true order but a
    repeatable one: the same input has to produce the same bundle on a retry, because a message
    that reshuffles itself between one send and the next reads as fresh news.
    """
    collapsed: dict[str, dict[tuple[ActivityKind, str | None], AgentActivity]] = {}
    for activity in activities:
        seen = collapsed.setdefault(activity.session_id, {})
        key = (activity.kind, activity.detail)
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


def _merged(
    carried: tuple[AgentActivity, ...], arrived: tuple[AgentActivity, ...]
) -> tuple[AgentActivity, ...]:
    """Fold new observations into what a standing message already says.

    Delegated to `grouped_for_delivery` rather than re-implemented, because the two rules that
    matter here are its rules: identical `(kind, detail)` pairs collapse to the newest, and
    what survives is ordered by `observed_at`. A second copy of them would drift, and the
    drift would be invisible -- a message whose lines are subtly out of order reads as the
    service having confused two sessions, which is exactly what that function's docstring is
    about.

    Both arguments belong to one session, so there is exactly one group to unpack.
    """
    groups = grouped_for_delivery((*carried, *arrived))
    return groups[0].activities if groups else ()


def _for_update(
    session_id: str,
    carried: tuple[AgentActivity, ...],
    arrived: tuple[AgentActivity, ...],
) -> SessionGroup:
    """Lay out a re-render so the lines nobody has seen yet are the ones it spells out.

    `shown_in_message` takes the newest five, which is right for a message being sent for the
    first time and wrong for one being amended. An observation that arrives *older* than five
    the message already carries -- a `needs_answer` queued behind five newer `completed`
    reports -- would be folded into "and N earlier" on this pass, and on every pass after it,
    forever: the merge keeps putting it back in the same losing position. Under the old shape
    it escaped because the next pass sent it as a message of its own. There is no next message
    now, so the room has to be made here, and the drain deleted its record long ago.

    So arrivals claim slots first, the previously-shown fill what is left, and the result is
    laid out with those slots **last**, because the end of the tuple is where
    `shown_in_message` looks. Ordering within the shown set stays by `observed_at`, so the
    message still reads as a timeline.
    """
    fresh = tuple(activity for activity in carried if activity in arrived)
    keep: list[AgentActivity] = list(fresh[-_MAXIMUM_LINES_PER_MESSAGE:])
    for activity in reversed(carried):
        if len(keep) >= _MAXIMUM_LINES_PER_MESSAGE:
            break
        if activity not in keep:
            keep.append(activity)
    shown = sorted(keep, key=lambda activity: activity.observed_at)
    buried = [activity for activity in carried if activity not in keep]
    return SessionGroup(session_id, (*buried, *shown))


def _told(
    arrived: tuple[AgentActivity, ...], shown: tuple[AgentActivity, ...]
) -> tuple[ActivityKind, ...]:
    """The kinds this pass both heard and put in front of the owner.

    The rate limit's question is how often a session *reports* a kind, so a line the message
    is merely still displaying is not an answer to it. See the call site for why narrowing to
    this is not the narrowing `_record_sent` warns against.
    """
    return tuple(activity.kind for activity in shown if activity in arrived)


def _unsaid(
    arrived: tuple[AgentActivity, ...], shown: tuple[AgentActivity, ...]
) -> tuple[AgentActivity, ...]:
    """Which of this pass's arrivals the message did not spell out, and therefore still owes.

    Measured against the arrivals rather than against everything the message accounts for,
    because those are two different sets once a message can be re-rendered. An observation
    that has dropped out of the newest five was *shown* on an earlier pass -- it has been
    told, and re-queueing it would print it a second time. One that arrived now and did not
    fit has been told to nobody, and the drain has already deleted its record, so letting it
    go loses agent output permanently.
    """
    return tuple(activity for activity in arrived if activity not in shown)


class ActivityNotifier:
    """Send each observation to the owner, at least once, and never as a storm.

    *At least once*, not exactly once, and the difference is not pedantry. The Bot API offers
    no idempotency key on `sendMessage`, so a send that succeeds on Telegram's side and then
    times out on the way back is indistinguishable here from one that never landed -- and the
    only safe answer to that ambiguity is to retry, which can duplicate. The rate limit is what
    bounds the consequence: a duplicate of the same kind for the same session inside the window
    is collapsed. Claiming "exactly once" would be claiming a guarantee the transport does not
    offer.

    It holds three pieces of state, and each answers a failure the others cannot:

    - **`_pending`** is the retry queue. `drain_activity` deletes a record before returning it,
      so an activity that reaches this object and is not sent exists nowhere else. A send that
      Telegram refuses therefore leaves the activity at the head of the queue rather than
      dropping it, and the next pass tries again. *Nowhere else* includes disk: DEC-026 keeps
      this queue in memory with no durable counterpart, so a restart takes whatever it is
      holding with it.
    - **`_last_sent`** is the rate limit, keyed by session *and* kind.
    - **`_bot`** arrives after construction. The boundary is built by the composition root
      long before there is a Telegram application to speak through, and a pass that runs
      before `attach` holds its activities rather than losing them.

    Delivery is **send, then mint, then attach the keyboard**, which is the one ordering with
    no race in it. `bind_pending` adopts every unbound token in the chat, so a token minted
    before an awaited send can be claimed by a render that interleaved with it -- binding the
    notification's button to the live view, where nothing draws it, and binding the live view's
    buttons to a notification. Minting against a message id that already exists closes that
    window rather than narrowing it.
    """

    def __init__(
        self,
        *,
        view: LiveView,
        callbacks: CallbackStatePort,
        owner_user_id: int,
        display: Callable[[str], Awaitable[str | None]],
        rate_limit_seconds: float = _RATE_LIMIT_SECONDS,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._view = view
        self._callbacks = callbacks
        self._owner_user_id = owner_user_id
        self._display = display
        self._rate_limit = timedelta(seconds=rate_limit_seconds)
        self._now = now
        self._bot: object | None = None
        self._pending: deque[AgentActivity] = deque()
        #: Whether the pass that just ran delivered anything, so `_report_backlog` can tell an
        #: outage from an ordinary pass that merely deferred a group past the per-pass ceiling.
        self._sent_this_pass = False
        self._last_sent: dict[tuple[str, ActivityKind], _Sent] = {}
        self._standing: dict[str, _Standing] = {}

    def attach(self, bot: object) -> None:
        """Learn which Telegram application to speak through, once there is one."""
        self._bot = bot

    def forget(self, session_id: str) -> None:
        """Give up the standing message for a session, so its next news starts a new one.

        Called when the message has left the chat: the owner pressed its button and `service`
        discarded it. Without this the next report would send a replacement and then try to
        delete a message that is already gone -- harmless, since `discard` treats "already
        gone" as the wanted state, but it would also carry the consumed message's lines into
        the new one. The owner has read those and acted on them; the new message is about
        what has happened since.
        """
        self._standing.pop(session_id, None)

    async def deliver(self, activities: Iterable[AgentActivity]) -> int:
        """Take a pass's observations and answer how many reached the owner.

        Never raises. This runs on the periodic task beside the one that serves the owner, and
        a notification failing is not a reason for the service to stop noticing things.
        """
        self._enqueue(activities)
        if self._bot is None:
            return 0
        self._forget_expired_limits()
        self._sent_this_pass = False
        # Grouped from the *whole* queue rather than from this pass's arrivals, which is what
        # makes a held group merge with news that came in since: an activity Telegram refused
        # last pass and one spooled a minute later belong to the same session and must leave
        # as one message, or the grouping has bought the owner nothing.
        held: list[AgentActivity] = []
        sent = 0
        refused = False
        for group in grouped_for_delivery(self._pending):
            if refused or sent >= _MAXIMUM_SENDS_PER_PASS:
                held.extend(group.activities)
                continue
            try:
                delivered, unsaid = await self._send(group)
            except Exception:
                # Held whole, and the pass stops. The record is already off disk -- the drain
                # deletes before it returns (DEC-013 cost 3) and DEC-026 keeps this queue in
                # memory with nothing behind it -- so an activity neither sent nor held here is
                # gone outright. Stopping rather than skipping to the next session keeps the
                # order and avoids hammering a Telegram that just refused us.
                _LOG.warning("could not deliver an activity notification; holding it for retry")
                held.extend(group.activities)
                refused = True
                continue
            sent += int(delivered)
            # What the message could not spell out is owed, not spent.
            held.extend(unsaid)
        self._sent_this_pass = sent > 0
        # What is held is the *collapsed* set, not what arrived: two identical observations are
        # one thing said twice, and re-holding both would resurrect a duplicate the next pass
        # has already been told to fold.
        self._pending = deque(held)
        if sent:
            # Once per pass, not once per message: the menu only has to end up below the last
            # notification, and moving it five times to get there would delete and re-send the
            # owner's screen five times.
            try:
                await self._view.move_to_bottom(self._bot)
            except Exception:
                # The notifications are delivered and that is the part that matters. A menu
                # that stayed where it was is the behaviour of every build before this one.
                _LOG.warning("could not move the live view below the notifications")
        self._report_backlog()
        return sent

    def pending_count(self) -> int:
        """How many observations are waiting on a Telegram that is not answering."""
        return len(self._pending)

    def _report_backlog(self) -> None:
        """Say, every pass, that undelivered notifications are being held in this process.

        The queue is process-local and there is no durable counterpart, which is a deliberate
        limitation rather than an oversight -- the spool it came from deletes before it
        delivers, so the same "lost on a crash" cost is already accepted one layer down.
        DEC-026 is where that cost was looked at again at the size an outage reaches, a hundred
        held notifications rather than the one DEC-013 reasoned about, and left where DEC-013
        recorded it: the session itself is the authoritative record of what an agent did, so
        what a restart destroys is the owner being *told* rather than the fact. What was
        missing was any way for an operator to *know* it applies right now: a restart
        during an outage takes the whole backlog with it, and nothing said the backlog existed.
        Saying it on every pass makes the window visible in the journal while it is open,
        rather than inferable afterwards from a notification that never came.
        """
        if self._pending and not self._sent_this_pass:
            # Guarded on *nothing having been sent*, because an ordinary pass that deferred a
            # group past the per-pass ceiling also leaves a non-empty queue, and warning on
            # that made the line fire on eight of forty healthy passes in simulation -- which
            # is how a warning added to make an outage visible becomes something an operator
            # learns to scroll past.
            _LOG.warning(
                "holding %d undelivered notification(s) in memory; a restart now loses them",
                len(self._pending),
            )

    def _enqueue(self, activities: Iterable[AgentActivity]) -> None:
        """Take a pass's observations, dropping the loudest session's oldest when full.

        **Not the queue's oldest, which is what this did.** Delivery is per session and so is
        fairness -- `grouped_for_delivery` orders by first appearance precisely so a burst
        cannot starve a quiet session -- but retention was global and per observation, so one
        chatty session could own all hundred slots and evict every other session's news from
        the head. Simulated: five sessions each reporting `QUIET` once, against one session
        emitting twenty-five distinct records a pass, ended with the queue holding a hundred
        observations from the loud session and nothing from the other five. Their reports were
        destroyed permanently -- `observe_quiet` fires once per spell and re-arms only on a
        pane change, so there is no second chance, and the drain had already deleted the files.

        Evicting from the session with the most queued observations makes the cap cost the
        session that filled it. Its *oldest* goes, because within one session the newest news
        is the news worth keeping.
        """
        for activity in activities:
            if len(self._pending) >= _MAXIMUM_PENDING:
                self._evict()
            self._pending.append(activity)

    def _evict(self) -> None:
        """Drop one observation from whichever session is using the most of the queue."""
        counts: dict[str, int] = {}
        for held in self._pending:
            counts[held.session_id] = counts.get(held.session_id, 0) + 1
        loudest = max(counts, key=lambda session_id: counts[session_id])
        for index, held in enumerate(self._pending):
            if held.session_id == loudest:
                del self._pending[index]
                break
        _LOG.warning(
            "the notification queue is full; dropping the oldest held for one session (%d held)",
            counts[loudest],
        )

    async def _send(self, group: SessionGroup) -> tuple[bool, tuple[AgentActivity, ...]]:
        """Deliver one session's news as one message, and answer what is still owed.

        Returns whether a message went out, and the observations it did *not* spell out --
        which the caller holds for the next pass rather than dropping. A group larger than
        `_MAXIMUM_LINES_PER_MESSAGE` is folded into "and N earlier", and those observations
        have then been neither shown nor sent; discarding them loses agent output permanently,
        since the drain deleted the record before it ever reached this queue.

        Declining is not failing: a group the rate limit emptied and a session that can no
        longer be named are both finished business, so the caller drops them rather than
        holding them. Only a raise means "try again".

        **The window decides whether a message is sent, never which lines it carries.** This
        is the correction to the shape this task first had, and the distinction is the whole
        of two defects. The limit exists to stop *messages*: one per session per pass is the
        cost the owner feels, and a second line inside a message they are already receiving
        costs them nothing. Filtering the group by window instead meant that an observation
        the queue was holding -- something the agent said that had never been sent, since
        anything still queued by definition has not been -- was *deleted* while a message to
        that very session went out with four of its five lines unused. Before grouping that
        trade was forced, because carrying it meant a second message; grouping removed the
        reason and the filter outlived it.

        The second defect is worse and is why this is stated as a rule. A kind held back by
        its own window was absent from the message, and `_record_sent` read that absence as
        "this session reported something different, so the held kind is not repeating" -- the
        notifier taking its own suppression decision as evidence against itself. A standing
        condition backed off to sixty-four minutes is absent from sixty-three of every
        sixty-four minutes' messages, so it was nearly always the kind that got reset, and its
        backoff never advanced past the first step. Measured on the module's own premises
        (`Stop` fires per turn), an overnight session with a periodic companion produced 75 to
        255 notifications where the taper intends 12. Sending the whole group closes both: what
        was observed rides along, and what was observed is what `_record_sent` is told about.

        **It closes them and does not restore the taper**, which is worth stating here because
        the numbers above invite the opposite reading. `_forget_expired_limits` prunes an entry
        at `_window(repeats) * _RETENTION_WINDOWS`, which at zero repeats is four minutes -- so
        a kind observed less often than that finds no entry, is re-created at zero, and never
        doubles at all. Measured after this change: a lone `Stop` every four minutes still
        produces 120 messages in eight hours. That defect is older than this module's grouping
        and is `_forget_expired_limits`' to answer, not this method's; it is named here so the
        next reader does not take "the storm is closed" for "the backoff works".
        """
        moment = self._now()
        standing = self._standing.get(group.session_id)
        # The window gates **every** send, a replacement included, and that is the whole
        # difference between this delivery shape and editing in place. An edit is silent, so
        # withholding one would only make the message stale; a replacement is a `sendMessage`
        # and lands on the owner's phone exactly like a first notification does. Measured with
        # the gate lifted for standing messages: an agent finishing a turn every five minutes
        # produced 96 notifications overnight -- one message in the chat, and the buzzing the
        # taper exists to stop.
        if not any(self._due(activity, moment) for activity in group.activities):
            return False, ()
        display = await self._display(group.session_id)
        if display is None:
            # Two refusals arrive as one `None`, and this module is deliberately unable to
            # tell them apart: the session cannot be named, or it is no longer one worth
            # speaking about. Both are finished business rather than a failed send, so both
            # get the same treatment here -- dropped, not held for retry. Which of the two it
            # was is decided and logged by the boundary that owns the lifecycle
            # (`service._display_for`), because a notifier that branched on session state
            # would be a driver adapter making lifecycle policy (DEC-001).
            _LOG.info("dropping an activity this service will not speak about")
            return False, ()

        if standing is not None:
            carried = _merged(standing.activities, group.activities)
            if carried == standing.activities:
                # The re-render would say exactly what the message already says -- the burst
                # case, an agent repeating one sentence. Nothing is sent, and nothing is held
                # either: the observations are already on the owner's screen, so they are
                # finished business rather than a debt. This is the collapse the rate limit
                # used to perform, now performed by comparing against what was actually said.
                return False, ()
            updated = _for_update(group.session_id, carried, group.activities)
            shown = shown_in_message(updated)
            replacement = await self._replace(standing, updated, display=display)
            if replacement is not None:
                message_id, token = replacement
                self._standing[group.session_id] = _Standing(message_id, shown, token)
                # Stamped for what *arrived* and was shown, not for everything on screen. The
                # two were the same thing when a message was built from one pass's news and
                # thrown away after; a standing message goes on displaying every kind it has
                # ever carried, so reading the screen would report each of them as reported
                # again on every pass -- the counts would climb without the agent saying
                # anything, and `_record_sent`'s cross-kind reset could never fire, because no
                # kind is ever absent from a message that keeps them all.
                #
                # This is a narrowing, and `_record_sent`'s docstring warns about one. It is
                # not that one: the argument there was narrowed by *suppression*, so the
                # notifier read its own silence as the session having changed the subject.
                # This narrows to what the session actually said this pass, which is the
                # question the taper was always asking.
                self._record_sent(group.session_id, _told(group.activities, shown), moment)
                return True, _unsaid(group.activities, shown)
            # The replacement could not be given a working button, which is the one outcome
            # worth starting over for: `_replace` has already put the message in the chat, so
            # falling through would leave two. Forgetting instead means the *next* pass sends
            # a fresh one, and the buttonless message is superseded then rather than now.
            self._standing.pop(group.session_id, None)
            return True, _unsaid(group.activities, shown)

        message_id = await self._view.send_apart(
            self._bot,
            {
                "text": activity_text(group, display=display),
                "parse_mode": ParseMode.HTML,
            },
        )
        # Recorded before the keyboard, because by here the owner has already been told. A
        # markup failure below must not re-send the message it is trying to decorate.
        #
        # Stamped for what was *rendered*, never for the whole group. A kind folded into "and
        # N earlier" has not been told to anyone, and stamping it would both silence it and
        # suppress its next report. What it does cover is a kind that was rendered without
        # being due -- that one has now been said, and is a repeat from here on.
        shown = shown_in_message(group)
        self._record_sent(group.session_id, (a.kind for a in shown), moment)
        token = await self._mint(group, message_id, display=display)
        if token is not None:
            # Recorded only once the button exists, because a standing message is one this
            # object will later *replace*, and a replacement carries this token onto the new
            # message. A message remembered without one would hand its missing button to every
            # message after it, where a message not remembered is merely superseded by the next
            # piece of news -- degraded once instead of degraded for good.
            self._standing[group.session_id] = _Standing(message_id, shown, token)
        return True, _unsaid(group.activities, shown)

    async def _replace(
        self, standing: _Standing, group: SessionGroup, *, display: str
    ) -> tuple[int, str] | None:
        """Say a session's news in a new message, take the old one out, and answer where it is.

        **Send, rebind, delete** -- `LiveView.move_to_bottom`'s order, and its argument holds
        here for the same reason: at every point between the steps, some message in the chat
        carries buttons that work. Sending first also means a refusal leaves the owner holding
        the message they already had rather than nothing, and a raise is the right answer to
        one: `deliver` holds the group and the next pass tries again.

        A replacement rather than an edit because an edit is silent and stays where it was
        sent. This costs a notification per update and a message that keeps arriving at the
        bottom of the chat, which is the trade the owner asked for: the session still occupies
        exactly one message, and the one it occupies is the newest thing in the chat.

        Answers `None` when the new message could not be given a working button. That is not
        the same as a failed send -- the message is in the chat and says the right thing -- so
        the caller starts the session over rather than sending a third.
        """
        rendered = render_activity(group, display=display, open_session=standing.token)
        message_id = await self._view.send_apart(
            self._bot,
            {
                "text": rendered.text,
                "parse_mode": ParseMode.HTML,
                "reply_markup": _markup(rendered.keyboard),
            },
        )
        token = standing.token
        if not self._callbacks.rebind(self._view.chat_id, standing.message_id, message_id):
            # Nothing moved, so the keyboard just sent resolves for a message that is about to
            # be deleted -- a dead button. Reachable without the old message having been acted
            # on: the store is bounded by size and evicts oldest-first, so a long-lived
            # notification's token can be collected out from under it.
            token = await self._mint(group, message_id, display=display)
            if token is None:
                return None
        try:
            await self._view.discard(self._bot, standing.message_id)
        except Exception:
            # The owner has the news. A delete that fails leaves the superseded message in the
            # chat -- untidy, and strictly better than what letting this escape would do:
            # `deliver` would call the whole group undelivered and re-queue it, so the next
            # pass would send the same notification again. Two messages either way, and this
            # way the duplicate is not also announced.
            _LOG.warning("could not remove the notification a replacement supersedes")
        return message_id, token

    async def _mint(self, group: SessionGroup, message_id: int, *, display: str) -> str | None:
        """Give a message that already exists its Open session button, or answer that it has none.

        Shared by the first send and by a replacement whose rebind found nothing to move. The
        guard is wide on purpose, and the width is the point: an earlier version wrapped only
        the API call, so a raise from the mint or the render escaped into `deliver`, which
        logged "holding it for retry" over a notification the owner had already received.
        """
        try:
            token = self._callbacks.create(
                # Spelled apart from `session.detail` so a press on this message cannot make
                # it the chat's live view -- see `service._NOTIFIED_DETAIL`. It opens the same
                # screen; it just says where the thumb was.
                NOTIFIED_DETAIL_ACTION,
                group.session_id,
                self._owner_user_id,
                self._view.chat_id,
                message_id,
            )
            rendered = render_activity(group, display=display, open_session=token)
            await self._bot.edit_message_reply_markup(
                chat_id=self._view.chat_id,
                message_id=message_id,
                reply_markup=_markup(rendered.keyboard),
            )
        except Exception:
            # The words are what the notification is for; the button is how it is convenient.
            # A message that arrived without one is degraded, and re-sending it to fix that
            # would be the storm this class exists to prevent.
            _LOG.warning("an activity notification was sent without its Open session button")
            return None
        return token

    def _due(self, activity: AgentActivity, moment: datetime) -> bool:
        """Whether this one observation is news, given when its kind was last sent."""
        entry = self._last_sent.get((activity.session_id, activity.kind))
        return entry is None or moment - entry.sent_at >= self._window(entry.repeats)

    def _window(self, repeats: int) -> timedelta:
        """How long this news stays old, given how many times it has already been sent.

        A fixed window is the right answer for a burst and the wrong one for a **standing**
        condition. `Stop` fires per turn, so a busy agent repeats "finished" and one message
        per two minutes is a fair summary. But `needs_answer` repeats for as long as the owner
        does not answer -- which, at three in the morning, is all night -- and a fixed window
        turns that into a message every two minutes until they wake up. The pane-quiet path
        already has the equivalent rule (`observe_quiet` reports once per spell and re-arms only
        on a change); the hook-sourced kinds had nothing, because the burst was the only case
        anyone had in mind.

        So the window doubles per consecutive repeat, capped: 2 minutes, 4, 8, 16, 32, then
        every 64 minutes for as long as it lasts. The first message arrives as fast as ever --
        this only ever makes the *second and later* copies rarer -- and the cap keeps the signal
        alive rather than muting it, because an agent that is still waiting is still news.
        """
        return self._rate_limit * (2 ** min(repeats, _MAXIMUM_BACKOFF_DOUBLINGS))

    def _record_sent(
        self, session_id: str, kinds: Iterable[ActivityKind], moment: datetime
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
        """
        carried = set(kinds)
        for kind in carried:
            key = (session_id, kind)
            prior = self._last_sent.get(key)
            self._last_sent[key] = _Sent(moment, 0 if prior is None else prior.repeats + 1)
        for other, sent in self._last_sent.items():
            if other[0] == session_id and other[1] not in carried and sent.repeats:
                self._last_sent[other] = _Sent(sent.sent_at, 0)

    def _forget_expired_limits(self) -> None:
        """Keep the rate-limit map the size of what it is still suppressing.

        One entry per (session, kind) is small, but it is unbounded over the life of a service
        that launches sessions all day, and an entry older than its window suppresses nothing.

        Measured against **its own** window rather than the base one. Under a fixed horizon a
        backed-off entry -- the ones that matter, because they are the repeating ones -- was
        forgotten while it was still suppressing, which silently restored the every-two-minutes
        behaviour the backoff exists to remove, and did it only for standing conditions.

        And kept for `_RETENTION_WINDOWS` times that, because the repeat count has to outlive
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
        """
        moment = self._now()
        floor = self._window(_MAXIMUM_BACKOFF_DOUBLINGS)
        expired = [
            key
            for key, sent in self._last_sent.items()
            if moment - sent.sent_at
            >= max(self._window(sent.repeats) * _RETENTION_WINDOWS, floor)
        ]
        for key in expired:
            del self._last_sent[key]


def _markup(keyboard: tuple[tuple[Button, ...], ...]) -> InlineKeyboardMarkup:
    """The keyboard half of `service._reply_arguments`, which cannot be shared with it.

    `service` imports this module to build the notifier, so the dependency only runs one way.
    Duplicating eight lines of adapter glue is the cheaper of the two prices; the wording and
    the bounding, which are the parts worth having one copy of, are not duplicated.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(button.text, callback_data=button.callback_data)
                for button in row
            ]
            for row in keyboard
        ]
    )
