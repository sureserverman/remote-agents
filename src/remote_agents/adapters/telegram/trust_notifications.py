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

from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from remote_agents.adapters.telegram.notifications import _markup
from remote_agents.adapters.telegram.presenters import (
    Button,
    RenderedMessage,
    _validate_callback,
    render_message,
)
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
        f"{_HEADLINE}\n{record.display.rendered}\n"
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
        return render_message(f"⛔ <b>Closed without trusting.</b>\n{record.display.rendered}")
    _validate_callback(open_session)
    return render_message(
        f"✅ <b>Trusted. The agent is running.</b>\n{record.display.rendered}",
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
        for key, record in records.items():
            if record.state is SessionState.UNTRUSTED:
                await self._ask(key, record)
        for standing in await self._store.unsettled():
            record = records.get(str(standing.session_id))
            if record is None or record.state is SessionState.UNTRUSTED:
                # Still asking, or gone from the listing entirely. Neither is an answer, and
                # amending on the second would rewrite a message about a session this service
                # can no longer describe.
                continue
            await self._settle(standing, record)

    async def _ask(self, key: str, record: SessionRecord) -> None:
        if key in self._abandoned:
            return
        standing = await self._store.standing_for(record.session_id)
        if standing is not None and not standing.settled:
            return
        answerable = record.profile_id in TRUST_ANSWERABLE
        try:
            message_id = await self._view.send_apart(
                self._bot, {"text": _question_text(record), "parse_mode": ParseMode.HTML}
            )
        except (BadRequest, TelegramError):
            self._refusals[key] = self._refusals.get(key, 0) + 1
            if self._refusals[key] >= _REFUSALS_BEFORE_ABANDONING:
                self._abandoned.add(key)
                _LOG.warning(
                    "giving up on the folder-trust question for session %s after %d refusals; "
                    "it can still be answered from the session's own screen",
                    key,
                    _REFUSALS_BEFORE_ABANDONING,
                )
            return
        self._refusals.pop(key, None)
        # Remembered before the keyboard, because by here the owner has already been told. A
        # markup failure must not re-send the message it is trying to decorate.
        await self._store.remember(
            record.session_id, chat_id=self._view.chat_id, message_id=message_id
        )
        await self._attach_answers(record, message_id, answerable=answerable)

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


def _question_text(record: SessionRecord) -> str:
    """The question's words alone, for the send that precedes the keyboard."""
    return render_trust_question(record, answerable=False, decline="c1_" + "0" * 20).text
