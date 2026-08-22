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
from remote_agents.application.notification_policy import (
    Sent,
    SessionGroup,
    due,
    enqueue,
    for_update,
    forget_expired,
    grouped_for_delivery,
    merged,
    record_sent,
    shown_in_message,
    told,
    unheard,
    unsaid,
)
from remote_agents.ports.agent_activity import (
    MAXIMUM_DETAIL_CHARACTERS,
    ActivityConfidence,
    ActivityKind,
    AgentActivity,
)
from remote_agents.ports.callback_state import CallbackStatePort
from remote_agents.ports.standing_notification import (
    StandingNotification,
    StandingNotificationPort,
)

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

A backstop, and since `grouped_for_delivery` collapses a session's news on the *kind* it is out
of ordinary reach: there are fewer kinds than lines here, so a real group no longer overflows it
at all. Kept rather than deleted because the fold below is what a kind being added would need,
and because the two defects that fold answers -- a `needs_answer` dropped silently, and a kind
stamped as told to an owner who never saw it -- are properties of folding rather than of the
number. `tests/unit/adapters/telegram/test_notifications.py` drives the cap down to two to keep
exercising it.

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
    shown = shown_in_message(group, limit=_MAXIMUM_LINES_PER_MESSAGE)
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


class StandingNotificationStore:
    """The process-local sibling of
    :class:`~remote_agents.adapters.sqlite.standing_notification_store.SQLiteStandingNotificationStore`.

    For a composition with no database behind it. A notifier composed on this one sends a
    session a *second* notification after a restart instead of amending the one already in the
    chat; that is acceptable in a test or a scratch composition and is not what the service
    runs -- `_private_boundary` hands it the durable store, for the reason
    :class:`~remote_agents.ports.standing_notification.StandingNotificationPort` records.
    """

    def __init__(self) -> None:
        self._standing: dict[tuple[int, str], StandingNotification] = {}

    def standing(self, chat_id: int) -> tuple[StandingNotification, ...]:
        return tuple(
            notification
            for (chat, _session), notification in self._standing.items()
            if chat == chat_id
        )

    def notification(self, chat_id: int, session_id: str) -> StandingNotification | None:
        return self._standing.get((chat_id, session_id))

    def record(self, chat_id: int, notification: StandingNotification) -> None:
        if notification.message_id <= 0:
            raise ValueError("a standing notification must name a real Telegram message")
        self._standing[(chat_id, notification.session_id)] = notification

    def forget(self, chat_id: int, session_id: str) -> None:
        self._standing.pop((chat_id, session_id), None)


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
records inside `application.notification_policy.enqueue` before a single send was
attempted, and the drain had already unlinked
them from disk. `drain_activity` goes to real trouble to take the *oldest* records so its bound
cannot truncate whole sessions; the notifier then discarded exactly that oldest half. The two
bounds were set against each other across the seam. It is not imported, and the reason given
here used to be that an adapter importing the application layer would invert this project's
dependencies (DEC-001). That was false: `check_imports.py` has let the two *driver* adapters,
`telegram` and `tui`, reach `remote_agents.application` since the local terminal landed, and
this module imports `application.notification_policy` a few dozen lines above. The honest
reason is smaller. The two bounds answer different questions -- how much one drain hands over
in a call, how much this queue holds while Telegram is unreachable -- and they only happen to
want the same number today, so the coupling is written down where a reader meets it rather than
made structural. The drain's constant is still the one to look at if either moves, and if they
are ever meant to be one number then importing it is permitted and is the better answer.

That there is nothing *behind* this cap is DEC-026 rather than an omission. A durable queue was
weighed and declined: it buys back a convenience at the price of a schema migration and a second
spool that must then be drained, bounded and reasoned about forever. So this number is the only
bound there is, and what it turns away is dropped rather than spilled anywhere.
"""


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
    - **`_last_sent`** is the rate limit, keyed by session *and* kind. The map lives here;
      the rules for it -- the taper, the repeat counters, the retention floor -- live in
      `application/notification_policy` and are handed this map to apply. Residence is not
      policy, the same split DEC-026 makes for the queue below.
    - **`_standing`** is which message each session's notification is, and it is the one piece
      of this object's state that is *not* process-local. It has to outlive the process for the
      same reason the live view's anchor does: a restart that forgets which message a session
      owns sends it a second one on the session's next report, and leaves the first sitting in
      the chat with nobody able to collect it. That is a durable store rather than a dict
      (`StandingNotificationPort`), and it is not the durable queue DEC-026 declined -- nothing
      here is drained, and a row names a message the owner is already looking at.
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
        standing: StandingNotificationPort | None = None,
        finished: Callable[[tuple[str, ...]], Awaitable[tuple[str, ...]]] | None = None,
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
        self._last_sent: dict[tuple[str, ActivityKind], Sent] = {}
        self._standing = standing if standing is not None else StandingNotificationStore()
        #: Which of the sessions holding a notification have stopped being worth one, or None
        #: in a composition with no lifecycle to ask. See `retire_finished`.
        self._finished = finished

    def attach(self, bot: object) -> None:
        """Learn which Telegram application to speak through, once there is one."""
        self._bot = bot

    def forget(self, session_id: str, message_id: int | None = None) -> None:
        """Give up the standing message for a session, so its next news starts a new one.

        Called when the message has left the chat: the owner pressed its button and `service`
        discarded it. Without this the next report would send a replacement and then try to
        delete a message that is already gone -- harmless, since `discard` treats "already
        gone" as the wanted state, but it would also carry the consumed message's lines into
        the new one. The owner has read those and acted on them; the new message is about
        what has happened since.

        **`message_id` names the message that was actually discarded, and a mismatch means
        forget nothing.** A stale notification can outlive the record of it -- the chat holds
        whatever was sent before this table existed, and a mint that fails leaves a message
        with no record at all -- so the button the owner pressed is not always the button on
        the message this session currently owns. Forgetting on the strength of the session id
        alone then threw away the record of a message still standing in the chat, and the next
        report started a *third* one beside it: the very accumulation this is here to prevent,
        triggered by pressing the button on the leftover. Passed None, it forgets regardless,
        which is the older contract and is what a caller with no message in hand gets.
        """
        if message_id is not None:
            standing = self._recall(session_id)
            if standing is not None and standing.message_id != message_id:
                _LOG.info(
                    "a notification the owner pressed was not this session's current one; "
                    "the standing message is kept"
                )
                return
        self._standing.forget(self._view.chat_id, session_id)

    def _recall(self, session_id: str) -> StandingNotification | None:
        """Which message this session owns, whichever process put it there."""
        return self._standing.notification(self._view.chat_id, session_id)

    def _remember(
        self, session_id: str, message_id: int, activities: tuple[AgentActivity, ...], token: str
    ) -> None:
        self._standing.record(
            self._view.chat_id,
            StandingNotification(session_id, message_id, activities, token),
        )

    async def retire_finished(self) -> int:
        """Take out of the chat every notification whose session has stopped being one.

        A notification makes one claim -- *an agent was working, and it has stopped, and you
        may want to open it* -- and a session the owner has stopped, force-stopped or closed
        has already answered that. The message then sits in the chat offering to open something
        that has ended, and it goes on pushing the menu up the screen. `_display_for` has
        always refused to send a *new* one about such a session; what it could not do is
        collect the one already sent.

        **Asked of the lifecycle on every delivery pass, because the stop is not always
        ours.** The local console stops sessions in a different process against the same
        database (DEC-005's second writer), so a notifier that only heard about the presses it
        served would leave the console's stops standing forever -- and a stop that happened
        while this process was not running would be heard about by nobody. Polling the records
        covers all three with one path.

        `service` also calls this the moment it serves a stop itself. That is about timing and
        nothing more: it makes the owner watch their own stop take its notification with it
        rather than find it gone half a minute later, and removing it would cost the speed,
        never the guarantee.

        Only the *message* is retired. The observation stays in `agent_activity`, so the local
        feed still shows what the agent did -- the owner asked for the alert to go, not the
        history, and those are two different surfaces reading two different things.

        Never raises: this runs inside the delivery pass, and a sweep that fails is not a
        reason for notifications to stop being delivered.
        """
        if self._bot is None or self._finished is None:
            return 0
        try:
            held = self._standing.standing(self._view.chat_id)
            if not held:
                return 0
            finished = set(await self._finished(tuple(one.session_id for one in held)))
        except Exception:
            _LOG.warning("could not ask which notified sessions have finished")
            return 0
        retired = 0
        for notification in held:
            if notification.session_id not in finished:
                continue
            try:
                # Pruned before the delete, for `service`'s reason on the press path: a token
                # that outlives its message is the dead button the callback store exists to
                # make impossible.
                self._callbacks.prune_for_message(self._view.chat_id, notification.message_id)
                await self._view.discard(self._bot, notification.message_id)
                # Forgotten whether or not Telegram still had it: `discard` answers True for a
                # message that was already gone, and both of those mean this session owns no
                # message any more.
                self._standing.forget(self._view.chat_id, notification.session_id)
            except Exception:
                # Left standing, deliberately. The record is what says the message is ours to
                # collect, so dropping it here would strand the message in the chat with
                # nothing able to try again -- and the next pass is thirty seconds away.
                _LOG.warning("could not remove the notification of a session that has finished")
                continue
            retired += 1
        return retired

    async def deliver(self, activities: Iterable[AgentActivity]) -> int:
        """Take a pass's observations and answer how many reached the owner.

        Never raises. This runs on the periodic task beside the one that serves the owner, and
        a notification failing is not a reason for the service to stop noticing things.
        """
        for _session_id, held in enqueue(self._pending, activities, maximum=_MAXIMUM_PENDING):
            # The rule is the policy's; the sentence is this surface's (DEC-043). Word for word
            # what it was before the rule moved, including *not* naming the session -- the
            # policy now hands back who paid and it would be one interpolation to say so, which
            # is exactly why this is spelled out. Relocating a rule is not licence to reword the
            # line an operator greps for, and a nicer sentence is the easiest kind of behaviour
            # change to ship inside a refactor: no test sees it and the diff reads as cleanup.
            # BL-032 holds the improvement, to be taken on its own or not at all.
            #
            # One thing here *did* move: the line used to be written inside `_evict`, as each
            # deletion happened, and is now written once per batch after `enqueue` returns.
            # Text, count, order and the pre-deletion tally are identical and nothing else logs
            # in between, so no operator can tell -- but it is the only ordering change in the
            # whole relocation, and a comment this careful about what did not change should say
            # what did.
            _LOG.warning(
                "the notification queue is full; dropping the oldest held for one "
                "session (%d held)",
                held,
            )
        if self._bot is None:
            return 0
        forget_expired(self._last_sent, self._now(), rate_limit=self._rate_limit)
        # Before the sends, not after. A session the owner has just stopped may also be
        # holding an observation in the queue; retiring first means its message leaves the
        # chat and `_display_for` then declines the leftover, rather than the pass amending a
        # message one line above the one that deletes it.
        await self.retire_finished()
        self._sent_this_pass = False
        # Grouped from the *whole* queue rather than from this pass's arrivals, which is what
        # makes a held group merge with news that came in since: an activity Telegram refused
        # last pass and one spooled a minute later belong to the same session and must leave
        # as one message, or the grouping has bought the owner nothing.
        held: list[AgentActivity] = []
        sent = 0
        arrived_at_the_bottom = False
        refused = False
        for group in grouped_for_delivery(self._pending):
            if refused or sent >= _MAXIMUM_SENDS_PER_PASS:
                held.extend(group.activities)
                continue
            try:
                delivered, alerted, unsaid = await self._send(group)
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
            arrived_at_the_bottom = arrived_at_the_bottom or alerted
            # What the message could not spell out is owed, not spent.
            held.extend(unsaid)
        self._sent_this_pass = sent > 0
        # What is held is the *collapsed* set, not what arrived: two identical observations are
        # one thing said twice, and re-holding both would resurrect a duplicate the next pass
        # has already been told to fold.
        self._pending = deque(held)
        if arrived_at_the_bottom:
            # Once per pass, not once per message: the menu only has to end up below the last
            # notification, and moving it five times to get there would delete and re-send the
            # owner's screen five times.
            #
            # And only when something actually *arrived* at the bottom. A pass that merely
            # amended standing messages moved nothing below the menu, and moving it anyway
            # would delete and re-send the owner's screen -- a message arriving on their phone,
            # which is the whole cost the silent amendment was chosen to avoid.
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
        its own window was absent from the message, and `record_sent` read that absence as
        "this session reported something different, so the held kind is not repeating" -- the
        notifier taking its own suppression decision as evidence against itself. A standing
        condition backed off to sixty-four minutes is absent from sixty-three of every
        sixty-four minutes' messages, so it was nearly always the kind that got reset, and its
        backoff never advanced past the first step. Measured on the module's own premises
        (`Stop` fires per turn), an overnight session with a periodic companion produced 75 to
        255 notifications where the taper intends 12. Sending the whole group closes both: what
        was observed rides along, and what was observed is what `record_sent` is told about.

        **A repeat is written into the message the owner already has; only news they have not
        been alerted to is allowed to arrive (DEC-034).** This is the second correction the
        owner asked for, and it is about the phone rather than the chat. Replacing a standing
        message keeps the chat at one message per session -- the whole of the previous fix, and it
        worked -- but every replacement is a `sendMessage`, so an agent finishing a turn every
        few minutes still buzzed the phone every few minutes and left the owner watching one
        message jump to the bottom of the chat over and over. `unheard` is the test: a kind the
        standing message does not already carry has never been put in front of them and earns a
        message that arrives, while a fresher `completed` behind a `completed` is the same news
        in newer words and is amended in silently. The first alert is as fast as it ever was;
        what is taken away is the second and later copies of it.

        **It closes them, and the taper is now restored too** -- which is worth stating here
        because this paragraph used to say the opposite and was left behind by the fix. It read
        that retention prunes an entry at `window(repeats) * RETENTION_WINDOWS`, four minutes at
        zero repeats, so a kind observed less often than that never doubles at all, and that a
        lone `Stop` every four minutes "still produces 120 messages in eight hours". DEC-031's
        count-independent floor answered that, and the horizon has not been that expression on
        its own since. Measured against the policy as it stands, a lone `Stop` every four
        minutes now produces **12** messages in eight hours, which is what the taper intends;
        `application/notification_policy.forget_expired` is where the floor lives and
        `test_a_kind_reporting_slower_than_its_first_window_still_reaches_the_taper` is the run.
        """
        moment = self._now()
        standing = self._recall(group.session_id)
        # The window gates **every** delivery, an amendment included. It reads as too strict
        # for one that is silent -- withholding an edit only makes a message stale -- and the
        # cost it is really bounding is not the buzz: it is a Bot API call per session per
        # pass, on the same chat rate limit `_MAXIMUM_SENDS_PER_PASS` exists for, spent to
        # rewrite a sentence with a slightly newer one. The message the owner is looking at
        # already says the session stopped, which is what it is for.
        if not any(
            due(activity, self._last_sent, moment, rate_limit=self._rate_limit)
            for activity in group.activities
        ):
            return False, False, ()
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
            return False, False, ()

        if standing is not None:
            carried = merged(standing.activities, group.activities)
            if carried == standing.activities:
                # The re-render would say exactly what the message already says -- the burst
                # case, an agent repeating one sentence. Nothing is sent, and nothing is held
                # either: the observations are already on the owner's screen, so they are
                # finished business rather than a debt. This is the collapse the rate limit
                # used to perform, now performed by comparing against what was actually said.
                return False, False, ()
            updated = for_update(
                group.session_id,
                carried,
                group.activities,
                limit=_MAXIMUM_LINES_PER_MESSAGE,
            )
            shown = shown_in_message(updated, limit=_MAXIMUM_LINES_PER_MESSAGE)
            if not unheard(standing.activities, shown):
                # Nothing here the owner has not already been alerted to, so this is the same
                # news in newer words: written into the message they have rather than sent as
                # one more. An amendment that answers False is not a failed delivery to retry
                # -- the message has been deleted, or has passed the 48 hours Telegram lets a
                # bot edit for -- so the session stops standing and the fresh send below takes
                # it, carrying the whole story rather than only what arrived this pass.
                # Rendered whole, keyboard included, because `editMessageText` replaces a
                # message's markup with whatever it is given -- and given none, it takes the
                # keyboard away. Sending only the text would have left the owner an amended
                # notification with no way to open the session it is about, which is most of
                # what the message is for and would have degraded on the *second* report
                # rather than the first.
                amended = render_activity(updated, display=display, open_session=standing.token)
                if await self._view.amend_apart(
                    self._bot,
                    standing.message_id,
                    {
                        "text": amended.text,
                        "parse_mode": ParseMode.HTML,
                        "reply_markup": _markup(amended.keyboard),
                    },
                ):
                    self._remember(group.session_id, standing.message_id, shown, standing.token)
                    record_sent(
                        self._last_sent, group.session_id, told(group.activities, shown), moment
                    )
                    return True, False, unsaid(group.activities, shown)
                self._standing.forget(self._view.chat_id, group.session_id)
                return await self._send_afresh(updated, group, display=display, moment=moment)
            replacement = await self._replace(standing, updated, display=display)
            if replacement is not None:
                message_id, token = replacement
                self._remember(group.session_id, message_id, shown, token)
                # Stamped for what *arrived* and was shown, not for everything on screen. The
                # two were the same thing when a message was built from one pass's news and
                # thrown away after; a standing message goes on displaying every kind it has
                # ever carried, so reading the screen would report each of them as reported
                # again on every pass -- the counts would climb without the agent saying
                # anything, and `record_sent`'s cross-kind reset could never fire, because no
                # kind is ever absent from a message that keeps them all.
                #
                # This is a narrowing, and `record_sent`'s docstring warns about one. It is
                # not that one: the argument there was narrowed by *suppression*, so the
                # notifier read its own silence as the session having changed the subject.
                # This narrows to what the session actually said this pass, which is the
                # question the taper was always asking.
                record_sent(
                    self._last_sent, group.session_id, told(group.activities, shown), moment
                )
                return True, True, unsaid(group.activities, shown)
            # The replacement could not be given a working button, which is the one outcome
            # worth starting over for: `_replace` has already put the message in the chat, so
            # falling through would leave two. Forgetting instead means the *next* pass sends
            # a fresh one, and the buttonless message is superseded then rather than now.
            self._standing.forget(self._view.chat_id, group.session_id)
            return True, True, unsaid(group.activities, shown)

        return await self._send_afresh(group, group, display=display, moment=moment)

    async def _send_afresh(
        self,
        outgoing: SessionGroup,
        group: SessionGroup,
        *,
        display: str,
        moment: datetime,
    ) -> tuple[bool, bool, tuple[AgentActivity, ...]]:
        """Start a session's message over, saying `outgoing` and owing against `group`.

        Two groups because the two questions differ once a message can be amended. `outgoing`
        is what the new message says -- the whole story when a standing message has been lost,
        so nothing the owner never read is silently dropped on the way to a fresh one -- while
        `group` is what this pass actually heard, which is what the rate limit is asked about
        and what is still owed if a line did not fit.
        """
        message_id = await self._view.send_apart(
            self._bot,
            {
                "text": activity_text(outgoing, display=display),
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
        shown = shown_in_message(outgoing, limit=_MAXIMUM_LINES_PER_MESSAGE)
        record_sent(self._last_sent, group.session_id, (a.kind for a in shown), moment)
        token = await self._mint(outgoing, message_id, display=display)
        if token is not None:
            # Recorded only once the button exists, because a standing message is one this
            # object will later *replace*, and a replacement carries this token onto the new
            # message. A message remembered without one would hand its missing button to every
            # message after it, where a message not remembered is merely superseded by the next
            # piece of news -- degraded once instead of degraded for good.
            self._remember(group.session_id, message_id, shown, token)
        return True, True, unsaid(group.activities, shown)

    async def _replace(
        self, standing: StandingNotification, group: SessionGroup, *, display: str
    ) -> tuple[int, str] | None:
        """Say a session's news in a new message, take the old one out, and answer where it is.

        **Send, rebind, delete** -- `LiveView.move_to_bottom`'s order, and its argument holds
        here for the same reason: at every point between the steps, some message in the chat
        carries buttons that work. Sending first also means a refusal leaves the owner holding
        the message they already had rather than nothing, and a raise is the right answer to
        one: `deliver` holds the group and the next pass tries again.

        A replacement rather than an amendment because an amendment is silent and stays where
        it was sent, and this path is only reached for news the owner has not been alerted to
        (`unheard`). That is where the trade now sits: the session occupies exactly one
        message either way, and it is re-sent to the bottom of the chat only when there is
        something in it worth interrupting them for. A repeat never gets here.

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
