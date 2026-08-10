"""One message per chat, redrawn — the only way a screen reaches the owner."""

from __future__ import annotations

import logging

from telegram.error import BadRequest

from remote_agents.ports.callback_state import CallbackStatePort
from remote_agents.ports.chat_view import ChatViewPort

_LOG = logging.getLogger(__name__)

_UNMODIFIED = "Message is not modified"
"""Telegram's answer to an edit whose content is byte-identical to what is already there.

It is an *error*, not a no-op, and letting it reach a handler is the recorded gotcha behind
dead-looking buttons: the screen stays as it was while the handler unwinds, so the owner is
left pressing a keyboard whose render never completed.
"""


class ChatViewStore:
    """The process-local sibling of
    :class:`~remote_agents.adapters.sqlite.chat_view_store.SQLiteChatViewStore`.

    For a composition with no database behind it. A bot composed on this one re-sends its
    live view after a restart instead of redrawing the old one; that is acceptable in a
    test or a scratch composition and is not what the service runs.
    """

    def __init__(self) -> None:
        self._anchors: dict[int, int] = {}

    def anchor(self, chat_id: int) -> int | None:
        return self._anchors.get(chat_id)

    def record_anchor(self, chat_id: int, message_id: int) -> None:
        if message_id <= 0:
            raise ValueError("a live view must be anchored to a real Telegram message")
        self._anchors[chat_id] = message_id


class LiveView:
    """The chat's single screen, and the owner of every token drawn on it.

    Before this, each handler answered whatever arrived — a command got a reply, a press got
    an edit — so the chat accumulated screens and every one of them kept live buttons. Here
    there is one message id per chat and every render is that id being redrawn, whatever
    triggered it.

    The render order is **edit, then prune, then bind**, and it is the whole correctness
    argument inherited from Stage 1: a screen's tokens are minted unbound, so pruning the
    anchor first discards exactly the keyboard being replaced and never the one about to be
    drawn, and binding then hands the new tokens to the message that now carries them.
    """

    def __init__(
        self, *, chat_id: int, callbacks: CallbackStatePort, anchors: ChatViewPort
    ) -> None:
        self._chat_id = chat_id
        self._callbacks = callbacks
        self._anchors = anchors

    @property
    def chat_id(self) -> int:
        return self._chat_id

    def anchor(self) -> int | None:
        """The message this chat's screens are drawn into, or None before the first one."""
        return self._anchors.anchor(self._chat_id)

    def adopt(self, message_id: int) -> None:
        """Learn the anchor from a message already proven to carry this chat's keyboard.

        A token only resolves for the message it was bound to, so a press that resolves is
        proof that message is the live view. That is what makes this safe on a composition
        whose anchor was never recorded — an in-memory store, or a database written before
        `chat_views` had a reader.

        It never *moves* a recorded anchor. A press arriving from some older message would
        otherwise walk the live view backwards onto a screen this stage exists to have
        deleted, which is a worse answer than leaving the anchor where the last render put
        it.
        """
        if message_id > 0 and self._anchors.anchor(self._chat_id) is None:
            self._anchors.record_anchor(self._chat_id, message_id)

    async def render(self, bot, arguments: dict[str, object], *, retire: bool = True) -> int:
        """Draw `arguments` as the chat's live view, and answer which message that is.

        `retire=False` draws a screen that does **not** take ownership of the message's
        keyboard. That is for an interstitial shown *while* an action runs: the token the
        owner just pressed is still being processed, and it is one of the tokens a retiring
        render would discard. Pruning there kills the action mid-flight — the button
        resolves, the wait appears, and nothing happens.
        """
        anchor = self._anchors.anchor(self._chat_id)
        if anchor is None:
            return await self._send(bot, arguments, retire=retire)
        try:
            await bot.edit_message_text(chat_id=self._chat_id, message_id=anchor, **arguments)
        except BadRequest as error:
            if _UNMODIFIED not in str(error):
                raise
            # Nothing changed on screen, so nothing on screen may stop resolving: the
            # keyboard the owner is looking at is still the one bound to this message, and
            # pruning it here would kill the buttons this render was trying to preserve.
            _LOG.debug("live view render changed nothing; the screen already says this")
            return anchor
        if retire:
            self._retire(anchor)
        return anchor

    async def _send(self, bot, arguments: dict[str, object], *, retire: bool = True) -> int:
        message = await bot.send_message(chat_id=self._chat_id, **arguments)
        message_id = int(message.message_id)
        self._anchors.record_anchor(self._chat_id, message_id)
        if retire:
            # No prune even here: there was no anchor, so this message replaces nothing and
            # can be carrying no retired keyboard. Only the binding is owed.
            self._callbacks.bind_pending(self._chat_id, message_id)
        return message_id

    def _retire(self, message_id: int) -> None:
        self._callbacks.prune_for_message(self._chat_id, message_id)
        self._callbacks.bind_pending(self._chat_id, message_id)
