"""The folder-trust question as a standing message, and what it becomes once answered.

**One renderer, two callers.** The launch reply and the notification sent on its own say the same
thing about the same session, and saying it twice would be two wordings to keep in step — so
`_launch_reply` renders through here rather than beside it.

Barless by construction (DEC-032). The navigation bar is appended at one choke point, and a
notification is a message rather than a screen: pressing "Sessions" on a message that outlives
the screen it was sent from is a different act from pressing it on the screen itself. These
call `render_message` directly, which is the mechanism and not an oversight.

**No filesystem path, which is a deliberate departure from the plan's sketch.** That asked for
the folder in `<code>` beneath the headline, and the bot cannot honour it: `CatalogProject`
carries `opaque_id`, `name`, `area` and `group` and no path, and this surface renders host
paths nowhere. The session's display identity is what every other message here names it by,
so it is what this names it by too.
"""

from __future__ import annotations

import logging
from html import escape

from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from remote_agents.adapters.telegram.notifications import _markup
from remote_agents.adapters.telegram.presenters import (
    Button,
    RenderedMessage,
    _validate_callback,
    render_message,
)
from remote_agents.application.session_actions import state_word
from remote_agents.application.session_views import listed_in_sessions
from remote_agents.domain.models import SessionRecord, SessionState
from remote_agents.domain.trust import TRUST_ANSWERABLE
from remote_agents.ports.trust_notifications import (
    StandingTrustQuestion,
    TrustNotificationStore,
)

TRUST_LABEL = "✅ Trust this project"
DECLINE_LABEL = "⛔ Don't trust — close it"
OPEN_LABEL = "Open session"

_HEADLINE = "🔒 <b>Waiting to be trusted</b>"

_LOG = logging.getLogger(__name__)

#: DEC-049's three-strike rule, the same bound the activity notifier uses.
_REFUSALS_BEFORE_ABANDONING = 3

#: What one `_ask` did, so `pass_once` can apply DEC-049's rule over the whole pass.
_REFUSED = "refused"
_DELIVERED = "delivered"


def render_trust_question(
    record: SessionRecord,
    *,
    answerable: bool,
    trust: str | None = None,
    decline: str,
) -> RenderedMessage:
    """The question, with the answers this project can actually give for that agent.

    `answerable` is the profile half of the policy, decided by the caller rather than here:
    saying *yes* means typing into the agent's own dialog and is confined to the agents whose
    dialog this project reads, while saying *no* ends a session that never started and is
    available for all of them.

    **One button per row.** DEC-032 gives the two-wide shape to the stop row, which is the one
    place on this surface where two buttons sit side by side; a pair of answers drawn the same
    way would read as a pair of stops.
    """
    if answerable and trust is None:
        raise ValueError("an answerable trust question needs a trust callback")
    rows: list[tuple[Button, ...]] = []
    if answerable and trust is not None:
        _validate_callback(trust)
        rows.append((Button(TRUST_LABEL, trust),))
    _validate_callback(decline)
    rows.append((Button(DECLINE_LABEL, decline),))
    return render_message(
        # Escaped, because a session label is the owner's own words and this message is sent
        # with `parse_mode=HTML`. Every caller of `_message` escapes; moving the wording into
        # a shared renderer moved that responsibility here and it was briefly dropped -- a
        # label containing a tag makes Telegram refuse the send outright.
        f"{_HEADLINE}\n{escape(record.display.rendered)}\n"
        "The agent is asking whether this folder can be trusted. "
        "Nothing runs until you answer.",
        tuple(rows),
    )


def render_trust_settled(record: SessionRecord, *, open_session: str) -> RenderedMessage:
    """What the standing question becomes once it has an answer.

    **An amendment carries its keyboard** (DEC-034), so a trusted session's message keeps a way
    into the session it is about rather than becoming a paragraph the owner can do nothing
    with. A declined one carries none, and that is the same rule rather than an exception to
    it: there is no longer a session to open, and a button that answers "no longer available"
    is worse than no button.
    """
    # `listed_in_sessions`, not `state is ENDED`. Which sessions still exist as far as a
    # surface is concerned is one decision with one home (DEC-017), and a frontend answering
    # it inline is the exact shape `test_no_frontend_decides_for_itself_which_sessions_are_
    # listed` exists to catch — it caught this. The question here is that question: is there
    # still a session to open, or did answering *no* take it away.
    if not listed_in_sessions(record):
        return render_message(
            f"⛔ <b>Closed without trusting.</b>\n{escape(record.display.rendered)}"
        )
    _validate_callback(open_session)
    return render_message(
        f"{_settled_headline(record)}\n{escape(record.display.rendered)}",
        ((Button(OPEN_LABEL, open_session),),),
    )


class TrustNotifier:
    """Ask each untrusted session's question once, and amend it once it has an answer.

    **The pass is the whole of it, and it is deliberately state-driven rather than
    event-driven.** Nothing tells this object that a session entered `UNTRUSTED`: the launch
    that produced it may have come from the other surface, or from a process that has since
    restarted. So it reads the records, and the durable row is what stops it asking twice —
    which is also what makes it correct across a restart, the case the row exists for.

    Three properties, each answering a way this could be wrong:

    - **One message per session.** A standing row that is not settled means the question is
      already on the owner's phone, whatever this process remembers.
    - **A repeat is amended, not re-sent** (DEC-034, DEC-048). The owner is alerted once; the
      answer changes the message they already have rather than arriving beside it.
    - **Three consecutive refusals abandon it** (DEC-049), with one journal line. A chat that
      will not take the message is not hammered forever, and the drop goes to the journal
      rather than to the chat that just refused — telling the owner there would mean sending a
      message about a message that could not be sent.

    Delivery is **send, then mint, then attach the keyboard**, copied from `ActivityNotifier`
    for its reason and not for symmetry: `bind_pending` adopts every unbound token in the chat,
    so a token minted before an awaited send can be claimed by a render that interleaved with
    it. Minting against a message id that already exists closes that window rather than
    narrowing it.
    """

    def __init__(
        self,
        *,
        sessions: object,
        store: TrustNotificationStore,
        view: object,
        callbacks: object,
        owner_user_id: int,
    ) -> None:
        self._sessions = sessions
        self._store = store
        self._view = view
        self._callbacks = callbacks
        self._owner_user_id = owner_user_id
        self._bot: object | None = None
        #: Consecutive refusals per session. Consecutive, not cumulative: an outage that ends
        #: must not have spent the budget, or a chat that was briefly unreachable months ago
        #: would abandon its next question on the first hiccup.
        self._refusals: dict[str, int] = {}
        #: Sessions already given up on, so the journal line is written once rather than on
        #: every pass for as long as the record survives.
        self._abandoned: set[str] = set()
        #: Sessions whose message exists but whose bookkeeping did not finish -- the durable
        #: row, the keyboard, or both. **The owner already has this message**, so the session
        #: must not be sent another; what is owed is the rest of the delivery, and the next
        #: pass finishes it from here rather than starting again.
        #:
        #: Without it the two half-failures are silent and opposite. A `remember` that failed
        #: after a successful send leaves no row, so the next pass reads "never asked" and
        #: sends a *duplicate*. An `attach` that failed after a successful `remember` leaves a
        #: row, so the next pass reads "already asked" and skips -- stranding a question on
        #: the owner's phone with no buttons on it, permanently and with no retry.
        self._incomplete: dict[str, int] = {}
        #: Sessions whose question is already on the owner's screen, because the bot's own
        #: launch reply *is* the question (`service._trust_question`). Without this the pass
        #: finds an UNTRUSTED record with no standing row five seconds later and sends the
        #: same two buttons again, which is the "never sent twice" property failing on the
        #: one path where the owner is definitely looking.
        #:
        #: Process-local and deliberately not the durable row. The row would have to name the
        #: live view's message, and settling later *amends* that message -- which by then is
        #: whatever screen the owner has navigated to. Migration 12's `message_id NOT NULL`
        #: leaves no third option without a schema change.
        #:
        #: **Accepted cost, and an earlier comment here got it wrong.** It claimed losing this
        #: on a restart was harmless "because the screen is gone too". It is not: the live
        #: view's anchor and its callback tokens are both durable, precisely so a restart does
        #: not void the buttons in the chat. So after a restart the launch reply is still
        #: standing, still showing the question and still pressable, and this pass -- having
        #: lost the set and never written a row -- sends a second copy. One extra message in a
        #: narrow window, never silence, which is the direction to fail in.
        self._asked_on_screen: set[str] = set()

    def note_asked_on_screen(self, session_id: object) -> None:
        """The bot rendered the question itself, so this pass must not send it again."""
        self._asked_on_screen.add(str(session_id))

    def attach(self, bot: object) -> None:
        """Hand it the Telegram application, which arrives long after construction."""
        self._bot = bot

    async def pass_once(self) -> None:
        """Ask what is unasked, amend what has been answered, and leave the rest alone."""
        if self._bot is None:
            return
        records = {
            str(record.session_id): record for record in await self._sessions.list_sessions()
        }
        # **Refusals are collected, not counted, until the pass is over** -- DEC-049's clause
        # that a strike is only recorded when at least one other send *succeeded* in the same
        # pass. When nothing got through, the refusals are evidence about an outage rather
        # than about any session, and counting them abandons every standing question over a
        # Telegram hiccup. At a five-second cadence that is fifteen seconds to permanent
        # silence, which is the failure DEC-049 exists to prevent and is sharper here than it
        # is for the activity pass.
        refused: list[str] = []
        delivered = 0
        for key, record in records.items():
            if record.state is not SessionState.UNTRUSTED:
                continue
            outcome = await self._ask(key, record)
            if outcome is _REFUSED:
                refused.append(key)
            elif outcome is _DELIVERED:
                delivered += 1
        if delivered:
            for key in refused:
                self._strike(key)
        elif refused:
            _LOG.info(
                "no folder-trust question got through this pass (%d refused); treating it as "
                "an outage rather than as evidence about any one session",
                len(refused),
            )
        for standing in await self._store.unsettled():
            record = records.get(str(standing.session_id))
            if record is None or record.state is SessionState.UNTRUSTED:
                # Still asking, or gone from the listing entirely. Neither is an answer, and
                # amending on the second would rewrite a message about a session this service
                # can no longer describe.
                continue
            try:
                await self._settle(standing, record)
            except Exception:
                # Per session, not per pass. One chat refusing an amendment must not defer
                # every other session's answer by a whole interval.
                _LOG.exception(
                    "could not settle the folder-trust question for session %s", standing.session_id
                )

    def _strike(self, key: str) -> None:
        """Record one refusal against a session, and give up at the bound (DEC-049)."""
        self._refusals[key] = self._refusals.get(key, 0) + 1
        if self._refusals[key] >= _REFUSALS_BEFORE_ABANDONING:
            self._abandoned.add(key)
            _LOG.warning(
                "giving up on the folder-trust question for session %s after %d refusals; "
                "it can still be answered from the session's own screen",
                key,
                _REFUSALS_BEFORE_ABANDONING,
            )

    async def _ask(self, key: str, record: SessionRecord) -> str | None:
        """Send the question if it is owed, and finish delivering one that was half-sent."""
        if key in self._abandoned or key in self._asked_on_screen:
            return None
        message_id = self._incomplete.get(key)
        if message_id is None:
            standing = await self._store.standing_for(record.session_id)
            if standing is not None and not standing.settled:
                return
            try:
                message_id = await self._view.send_apart(
                    self._bot, {"text": _question_text(record), "parse_mode": ParseMode.HTML}
                )
            except (BadRequest, TelegramError):
                return _REFUSED
            self._refusals.pop(key, None)
            # **From here the owner has the message**, and everything after it is bookkeeping.
            # Recorded before either write so that a failure in one of them cannot be mistaken
            # for "never asked" on the next pass -- which would send a second copy of a
            # question the owner is already looking at.
            self._incomplete[key] = message_id
        try:
            await self._store.remember(
                record.session_id, chat_id=self._view.chat_id, message_id=message_id
            )
            await self._attach_answers(
                record, message_id, answerable=record.profile_id in TRUST_ANSWERABLE
            )
        except Exception:
            # Left in `_incomplete`, so the next pass finishes it rather than re-sending. The
            # store write is an idempotent upsert and the keyboard amend is an edit to a
            # message that already exists, so repeating either costs nothing.
            _LOG.exception(
                "the folder-trust question for session %s was sent but not fully delivered; "
                "the next pass will finish it",
                key,
            )
            return _DELIVERED
        self._incomplete.pop(key, None)
        return _DELIVERED

    async def _attach_answers(
        self, record: SessionRecord, message_id: int, *, answerable: bool
    ) -> None:
        session_value = str(record.session_id)
        rendered = render_trust_question(
            record,
            answerable=answerable,
            trust=(
                self._callbacks.create(
                    "session.trust",
                    session_value,
                    self._owner_user_id,
                    self._view.chat_id,
                    message_id,
                    mutation=True,
                )
                if answerable
                else None
            ),
            decline=self._callbacks.create(
                "session.decline",
                session_value,
                self._owner_user_id,
                self._view.chat_id,
                message_id,
                mutation=True,
            ),
        )
        await self._view.amend_apart(
            self._bot,
            message_id,
            {
                "text": rendered.text,
                "parse_mode": ParseMode.HTML,
                "reply_markup": _markup(rendered.keyboard),
            },
        )

    async def _settle(self, standing: StandingTrustQuestion, record: SessionRecord) -> None:
        rendered = render_trust_settled(
            record,
            open_session=self._callbacks.create(
                "session.detail",
                str(record.session_id),
                self._owner_user_id,
                self._view.chat_id,
                standing.message_id,
            ),
        )
        await self._view.amend_apart(
            self._bot,
            standing.message_id,
            {
                "text": rendered.text,
                "parse_mode": ParseMode.HTML,
                "reply_markup": _markup(rendered.keyboard),
            },
        )
        await self._store.settle(record.session_id)


def _settled_headline(record: SessionRecord) -> str:
    """What the amendment says happened, taken from the lifecycle rather than invented here.

    A session can leave `UNTRUSTED` for more than two destinations: the owner trusts it and it
    runs, or declines and it ends -- but its pane can also die, leaving FAILED or PRESERVED,
    or reconciliation can find it ambiguous and orphan it. Branching on "still listed" alone
    called every one of those "Trusted. The agent is running.", which asserts a fact the
    record does not carry.

    `state_word` is the single authority on what a state is called (DEC-029), so the
    non-running endings borrow it rather than growing a second vocabulary here.
    """
    if record.state is SessionState.RUNNING:
        return "✅ <b>Trusted. The agent is running.</b>"
    return (
        "🔓 <b>No longer waiting to be trusted.</b> "
        f"<code>{escape(state_word(record.state, record.orphan_provenance))}</code>"
    )


def _question_text(record: SessionRecord) -> str:
    """The question's words alone, for the send that precedes the keyboard."""
    return render_trust_question(record, answerable=False, decline="c1_" + "0" * 20).text
