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
this module's first rule: what reaches here is a permission prompt or an agent saying it needs
input, and in both the agent is the one making the claim.
"""

# The UTF-16 budget, the escape-then-fit routine and the callback shape are imported from
# `presenters` rather than copied, private names and all: an escaper and a budget that exist
# twice are two escapers and two budgets, and only one of them ever gets fixed.


def activity_text(activity: AgentActivity, *, display: str) -> str:
    """The message's words, which depend on nothing Telegram has to answer for first.

    Split out from `render_activity` because the delivery order is send-then-mint: the
    notification's token is bound to the message the send answers with, so the text has to
    exist before the token does. Nothing about the wording depends on the button, which is
    what makes that order available at all.

    The bounding order is detail first, then the name. Either can be pathological -- a display
    identity carries an owner-supplied label -- and reserving the detail's slot before fitting
    the name means a long name truncates itself rather than silently deleting what the agent
    said.
    """
    sentence = _sentence(activity)
    hedge = _HEDGE if activity.confidence is ActivityConfidence.INFERRED else ""

    detail = _bounded_escaped(_detail_of(activity) or "", MAXIMUM_DETAIL_CHARACTERS)
    tail = (f"\n{detail}" if detail else "") + (f"\n{hedge}" if hedge else "")

    skeleton = f"<b></b>\n{sentence}{tail}"
    name = _bounded_escaped(display, MAX_TELEGRAM_TEXT_UNITS - _utf16_units(skeleton))
    return f"<b>{name}</b>\n{sentence}{tail}"


def render_activity(activity: AgentActivity, *, display: str, open_session: str) -> RenderedMessage:
    """Render one observation as the whole message the owner receives about it.

    Pure, and deliberately ignorant of Telegram's transport: it is handed the session's
    display identity and an already-minted callback token because resolving either would mean
    this renderer reaching for a store, and the rendering is the part worth testing
    exhaustively.
    """
    _validate_callback(open_session)
    return render_message(
        activity_text(activity, display=display),
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

Ten per pass against a thirty-second poll is a third of a message per second, comfortably
under that. Nothing is dropped: the remainder stays queued and the next pass takes it, so a
genuine burst arrives spread out rather than refused. This is the only bound here that is
about the *chat* rather than about one session's news.
"""

_MAXIMUM_PENDING = 100
"""How many undelivered notifications are worth holding while Telegram is unreachable.

The drain has already deleted the spool files by the time a send is refused, so this queue is
the only remaining copy -- which is the argument for holding some, and the reason it cannot be
unbounded. An outage long enough to overflow this has produced news too stale to send anyway,
so the oldest is dropped and said out loud.

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
        self._last_sent: dict[tuple[str, ActivityKind], _Sent] = {}

    def attach(self, bot: object) -> None:
        """Learn which Telegram application to speak through, once there is one."""
        self._bot = bot

    async def deliver(self, activities: Iterable[AgentActivity]) -> int:
        """Take a pass's observations and answer how many reached the owner.

        Never raises. This runs on the periodic task beside the one that serves the owner, and
        a notification failing is not a reason for the service to stop noticing things.
        """
        self._enqueue(activities)
        if self._bot is None:
            return 0
        self._forget_expired_limits()
        sent = 0
        while self._pending and sent < _MAXIMUM_SENDS_PER_PASS:
            activity = self._pending[0]
            try:
                delivered = await self._send(activity)
            except Exception:
                # Left at the head deliberately: the record is already off disk, so dropping it
                # here loses it outright. Stopping the pass rather than skipping to the next
                # keeps the order and avoids hammering a Telegram that just refused us.
                _LOG.warning("could not deliver an activity notification; holding it for retry")
                break
            self._pending.popleft()
            sent += int(delivered)
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
        if self._pending:
            _LOG.warning(
                "holding %d undelivered notification(s) in memory; a restart now loses them",
                len(self._pending),
            )

    def _enqueue(self, activities: Iterable[AgentActivity]) -> None:
        for activity in activities:
            if len(self._pending) >= _MAXIMUM_PENDING:
                self._pending.popleft()
                _LOG.warning("dropping the oldest undelivered notification; the queue is full")
            self._pending.append(activity)

    async def _send(self, activity: AgentActivity) -> bool:
        """Deliver one observation, or decline to, and say which.

        Declining is not failing: a collapsed burst and a session that can no longer be named
        are both finished business, so the caller drops them. Only a raise means "try again".
        """
        moment = self._now()
        key = (activity.session_id, activity.kind)
        entry = self._last_sent.get(key)
        if entry is not None and moment - entry.sent_at < self._window(entry.repeats):
            return False
        display = await self._display(activity.session_id)
        if display is None:
            # Nothing to name it and nothing for its button to open. Rare -- the store outlives
            # a session -- and a message reading "a session has finished" is worse than silence.
            _LOG.info("dropping an activity for a session this service can no longer name")
            return False

        message_id = await self._view.send_apart(
            self._bot,
            {"text": activity_text(activity, display=display), "parse_mode": ParseMode.HTML},
        )
        # Recorded before the keyboard, because by here the owner has already been told. A
        # markup failure below must not re-send the message it is trying to decorate.
        self._record_sent(key, moment, entry)
        # The guard covers the mint and the render as well as the call, and the width is the
        # point. An earlier version wrapped only `edit_message_reply_markup`, which left two
        # raising steps outside it -- and a raise there escaped into `deliver`, which logged
        # "holding it for retry" over an activity the owner had *already received*. The next
        # pass then found the rate limit recorded one line above, declined it as a collapsed
        # burst, and popped it silently. Two wrong statements about one notification, from a
        # guard drawn one line too narrow. Everything after the send decorates a message that
        # has landed, so everything after the send is caught together.
        try:
            token = self._callbacks.create(
                # Spelled apart from `session.detail` so a press on this message cannot make
                # it the chat's live view -- see `service._NOTIFIED_DETAIL`. It opens the same
                # screen; it just says where the thumb was.
                NOTIFIED_DETAIL_ACTION,
                activity.session_id,
                self._owner_user_id,
                self._view.chat_id,
                message_id,
            )
            rendered = render_activity(activity, display=display, open_session=token)
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
        return True

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
        self, key: tuple[str, ActivityKind], moment: datetime, prior: _Sent | None
    ) -> None:
        """Stamp this send, and let anything else about the same session start over.

        A repeat count is a claim that *nothing has changed*. The moment a session reports a
        different kind, something has: an agent that finishes, is asked something, and finishes
        again is not repeating itself, and backing its second "finished" off to an hour would
        answer the wrong question. Only the counter resets -- the other kinds keep their stamps,
        so their base windows still collapse a genuine burst.
        """
        self._last_sent[key] = _Sent(moment, 0 if prior is None else prior.repeats + 1)
        session = key[0]
        for other, sent in self._last_sent.items():
            if other[0] == session and other != key and sent.repeats:
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
        """
        moment = self._now()
        expired = [
            key
            for key, sent in self._last_sent.items()
            if moment - sent.sent_at >= self._window(sent.repeats) * _RETENTION_WINDOWS
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
