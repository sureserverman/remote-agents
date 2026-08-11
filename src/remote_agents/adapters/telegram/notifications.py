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


_RATE_LIMIT_SECONDS = 120.0
"""How long one session's one kind of news stays old.

A `Stop` hook fires per turn, not per task, so an agent working through a long instruction
reports "finished" repeatedly and each report is true. The owner does not need five of them.
Scoped to (session, kind) rather than to the chat: an agent that finishes and then needs an
answer has said two different things, and collapsing those would lose the one worth acting on.
"""

_MAXIMUM_PENDING = 100
"""How many undelivered notifications are worth holding while Telegram is unreachable.

The drain has already deleted the spool files by the time a send is refused, so this queue is
the only remaining copy -- which is the argument for holding some, and the reason it cannot be
unbounded. An outage long enough to overflow this has produced news too stale to send anyway,
so the oldest is dropped and said out loud.
"""


class ActivityNotifier:
    """Send each observation to the owner once, and never turn a busy agent into a storm.

    It holds three pieces of state, and each answers a failure the others cannot:

    - **`_pending`** is the retry queue. `drain_activity` deletes a record before returning it,
      so an activity that reaches this object and is not sent exists nowhere else. A send that
      Telegram refuses therefore leaves the activity at the head of the queue rather than
      dropping it, and the next pass tries again.
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
        self._last_sent: dict[tuple[str, ActivityKind], datetime] = {}

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
        while self._pending:
            activity = self._pending[0]
            try:
                delivered = await self._send(activity)
            except Exception:
                # Left at the head deliberately: the record is already off disk, so dropping it
                # here loses it outright. Stopping the pass rather than skipping to the next
                # keeps the order and avoids hammering a Telegram that just refused us.
                _LOG.warning("could not deliver an activity notification; holding it for retry")
                return sent
            self._pending.popleft()
            sent += int(delivered)
        return sent

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
        last = self._last_sent.get(key)
        if last is not None and moment - last < self._rate_limit:
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
        self._last_sent[key] = moment
        token = self._callbacks.create(
            "session.detail",
            activity.session_id,
            self._owner_user_id,
            self._view.chat_id,
            message_id,
        )
        rendered = render_activity(activity, display=display, open_session=token)
        try:
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

    def _forget_expired_limits(self) -> None:
        """Keep the rate-limit map the size of what it is still suppressing.

        One entry per (session, kind) is small, but it is unbounded over the life of a service
        that launches sessions all day, and an entry older than the window suppresses nothing.
        """
        horizon = self._now() - self._rate_limit
        for key in [key for key, moment in self._last_sent.items() if moment < horizon]:
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
