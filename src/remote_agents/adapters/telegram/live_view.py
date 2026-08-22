"""One message per chat, redrawn — the only way a screen reaches the owner."""

from __future__ import annotations

import logging

from telegram.error import BadRequest

from remote_agents.ports.callback_state import CallbackStatePort
from remote_agents.ports.chat_view import ChatViewPort

_LOG = logging.getLogger(__name__)

_UNEDITABLE = (
    "message can't be edited",
    "message to edit not found",
    "message_id_invalid",
)
"""Telegram's ways of saying this particular message can no longer be written to.

`editMessageText` is documented to work for 48 hours, and since Stage 1 a token outlives its
message's editable life by design — so these are the expected end of a long-lived screen,
answered by moving the view rather than by an error. Every *other* `BadRequest` is a real
fault and still propagates: an unclosed HTML entity in the text, a keyboard past Telegram's
button limit. A bot blocked from its chat raises `Forbidden`, which this never catches at all.
"""


def _is_uneditable(error: BadRequest) -> bool:
    message = str(error).casefold()
    return any(refusal in message for refusal in _UNEDITABLE)


_ALREADY_GONE = "message to delete not found"
"""Telegram's answer to deleting something that is not there — which is the wanted state."""

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

    def adopt_anchor(self, chat_id: int, message_id: int) -> bool:
        if message_id <= 0:
            raise ValueError("a live view must be anchored to a real Telegram message")
        if chat_id in self._anchors:
            # False even when the stored anchor is this very id. SQLite's `DO NOTHING`
            # cannot tell "conflicted with the same value" from "conflicted with a
            # different one", and a port whose two implementations answer differently is
            # worse than one that answers the narrower thing consistently.
            return False
        self._anchors[chat_id] = message_id
        return True


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
        self._last_arguments: dict[str, object] | None = None
        """What the anchor currently shows, so the screen can be moved without re-deriving it.

        Process-local, and that is the right scope: it exists only to redraw the screen the
        owner is looking at *now*. A restart has no "now" to preserve — the first render after
        one fills this in, and until then `move_to_bottom` declines rather than guessing, which
        is why it is safe for this to be empty.
        """

        self._owed_prunes: set[int] = set()
        """Retired messages whose tokens an interstitial re-send could not discard yet.

        A set rather than one id, so a second interstitial re-send before the first has been
        collected cannot silently drop the first. Every retiring render drains the whole set.

        Process-local on purpose: a prune is owed only between an interstitial and the
        render that answers it, and a restart inside that window has already lost the action
        itself. Anything this drops is inert — the message it belonged to is deleted, so no
        button carries those tokens — and the store's size cap collects it.
        """

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
        it. That condition is the store's to enforce in one statement — DEC-005 permits a
        second writer, and asking then writing would let the loser overwrite the winner.
        """
        if message_id > 0:
            self._anchors.adopt_anchor(self._chat_id, message_id)

    async def render(self, bot, arguments: dict[str, object], *, retire: bool = True) -> int:
        """Draw `arguments` as the chat's live view, and answer which message that is.

        `retire=False` draws a screen that does **not** take ownership of the message's
        keyboard. That is for an interstitial shown *while* an action runs: the token the
        owner just pressed is still being processed, and it is one of the tokens a retiring
        render would discard. Pruning there kills the action mid-flight — the button
        resolves, the wait appears, and nothing happens.
        """
        # Remembered before the send, so a screen that is drawn and then fails to move is still
        # the screen this object believes it is showing.
        if retire:
            self._last_arguments = arguments
        anchor = self._anchors.anchor(self._chat_id)
        if anchor is None:
            return await self._send(bot, arguments, retire=retire)
        try:
            await bot.edit_message_text(chat_id=self._chat_id, message_id=anchor, **arguments)
        except BadRequest as error:
            if _UNMODIFIED in str(error):
                # Nothing changed on screen, so nothing on screen may stop resolving: the
                # keyboard the owner is looking at is still the one bound to this message,
                # and pruning it here would kill the buttons this render was trying to
                # preserve.
                _LOG.debug("live view render changed nothing; the screen already says this")
                return anchor
            if not _is_uneditable(error):
                raise
            return await self._resend(bot, arguments, retired=anchor, retire=retire)
        if retire:
            self._retire(anchor)
        return anchor

    async def _resend(
        self, bot, arguments: dict[str, object], *, retired: int, retire: bool
    ) -> int:
        """Answer an edit Telegram will not perform by moving the live view to a new message.

        `editMessageText` stops working 48 hours after a message was sent, and since Stage 1
        a button outlives that by design — so this is a reachable path, not a defensive one.
        The owner never learns it happened: they pressed a button and a screen appeared.
        """
        _LOG.info("live view message %d can no longer be edited; re-sending it", retired)
        message_id = await self._send(bot, arguments, retire=retire)
        await self._delete(bot, retired)
        if retire:
            self._drain_owed()
            self._callbacks.prune_for_message(self._chat_id, retired)
        else:
            # An interstitial: the token the owner just pressed is bound to `retired` and
            # its action has not been claimed yet, so pruning now is what makes a stop
            # resolve, show its wait screen, and never happen. Every retiring render drains
            # this, including one that is itself a re-send — which is why the drain is a
            # shared step rather than a line inside `_retire`.
            self._owed_prunes.add(retired)
        return message_id

    async def move_to_bottom(self, bot) -> int | None:
        """Redraw the current screen as the newest message in the chat, and answer where.

        Telegram orders a chat by send time and nothing else, so every notification lands
        *below* the live view and pushes it further out of sight. Editing the anchor in place
        cannot fix that — the message stays where it was sent — so the only way for the menu to
        stay reachable is for it to be sent again, below whatever has arrived since.

        The owner's report is the reason this exists: notifications accumulated, each one
        pushing the menu up, until reaching it meant scrolling past them. Sub-plan 1 gave this
        chat exactly one bot message so the screen was always the last thing in it; Stage 3
        added a second kind of message and quietly took that away again.

        Answers None when there is nothing to move — no anchor yet, or no remembered screen
        after a restart — because inventing a screen to move is worse than leaving one behind.

        The token rebind is what makes this safe rather than merely tidy. A token resolves only
        against the message it is bound to, so re-sending without moving them leaves the new
        keyboard entirely dead. Send, rebind, then delete: at every point between those steps
        some message in the chat carries buttons that work.
        """
        anchor = self._anchors.anchor(self._chat_id)
        if anchor is None or self._last_arguments is None:
            return None
        message = await bot.send_message(chat_id=self._chat_id, **self._last_arguments)
        moved = int(message.message_id)
        self._anchors.record_anchor(self._chat_id, moved)
        self._callbacks.rebind(self._chat_id, anchor, moved)
        await self._delete(bot, anchor)
        return moved

    async def send_apart(self, bot, arguments: dict[str, object]) -> int:
        """Send a message that is deliberately *not* the live view, and answer its id.

        Two things need this and nothing else should. A `ForceReply` cannot ride on an edit
        of a message carrying an inline keyboard — Telegram answers `Inline keyboard
        expected` — so a guided step's input box has to be its own message. And a captured
        document is a document; it cannot be a screen.

        It binds no tokens, which is the part worth being careful about: a `ForceReply` and
        a document are the only two things sent this way and neither carries an inline
        keyboard, so there is nothing of theirs to bind — while calling `bind_pending` here
        would hand them the live view's freshly minted tokens instead.

        Whatever this sends is the caller's to `discard` once it has served its purpose.
        """
        message = await bot.send_message(chat_id=self._chat_id, **arguments)
        return int(message.message_id)

    async def amend_apart(self, bot, message_id: int, arguments: dict[str, object]) -> bool:
        """Rewrite a message sent apart from the live view, leaving it where it is.

        The silent counterpart to `send_apart`, and silence is the whole point of it. A
        notification that re-sends itself arrives on the owner's phone again; one amended in
        place does not, so this is what a repeat of news the owner has already been alerted
        to is allowed to cost them.

        Answers whether the message now says this. `False` means Telegram will not write to
        that message any more -- it has been deleted, or it has passed the 48 hours
        `editMessageText` is documented for -- which is not a failure to retry but a message
        the caller must stop treating as standing. An edit that changed nothing is `True`:
        the message already says it, which is the wanted state and is exactly what the caller
        asked for. Every other `BadRequest` still propagates, because an unclosed entity or
        an oversized keyboard is a fault in what we built rather than a fact about the chat.

        It binds no tokens, for `send_apart`'s reason: the keyboard here was minted against
        this message id already, and `bind_pending` would hand it the live view's instead.
        """
        try:
            await bot.edit_message_text(chat_id=self._chat_id, message_id=message_id, **arguments)
        except BadRequest as error:
            if _UNMODIFIED in str(error):
                _LOG.debug("message %d already says this", message_id)
                return True
            if _is_uneditable(error):
                _LOG.info("message %d can no longer be edited", message_id)
                return False
            raise
        return True

    async def send_document_apart(self, bot, **arguments: object) -> int:
        """Send a captured file beside the live view, and answer its id.

        Addressed to the chat rather than replied to the anchor, because by the time this
        runs the anchor may have just been re-sent — replying to a message that no longer
        exists is a failure the owner would see, for a convenience nobody asked for.

        Binds nothing, for the same reason `send_apart` does not: a document carries no
        inline keyboard.
        """
        message = await bot.send_document(chat_id=self._chat_id, **arguments)
        return int(message.message_id)

    async def discard(self, bot, message_id: int) -> bool:
        """Take a message out of the chat, and answer for why it was allowed to go.

        Four things qualify, and the rule is worth stating because deletion is the one
        irreversible thing this adapter does. A message may be discarded when it is a
        **superseded screen of ours**; an **input of the owner's that has been consumed** — a
        command that has been answered, a reply that has been read; a **question of ours
        that nobody is going to answer**, an input box for a step the owner has walked away
        from; or a **captured document of ours the owner has navigated away from**, released
        when they leave the session it came out of. Nothing else: not a message the bot did
        not send and the owner did not just send, and never anything the owner might still
        be reading.

        The fourth was left out when this rule was first written, which made a docstring
        bounding an irreversible operation narrower than the operation — the released
        capture is ours and superseded, so it always belonged to the first category in
        spirit, but a rule that says "exactly three" and permits four is not a rule.

        Answers whether the message is actually gone. A caller tracking a message it must
        eventually remove needs to know the difference between "deleted" and "refused" —
        without it, the only record of a surviving message is dropped on the assumption it
        worked, and nothing can ever try again.
        """
        return await self._delete(bot, message_id)

    async def _delete(self, bot, message_id: int) -> bool:
        try:
            await bot.delete_message(chat_id=self._chat_id, message_id=message_id)
        except BadRequest as error:
            if _ALREADY_GONE in str(error).casefold():
                # Gone is what was wanted; something else got there first.
                _LOG.debug("message %d was already gone", message_id)
                return True
            # Not the same thing at all: Telegram still has it and will not remove it, so
            # the message stays in the chat. Worth saying out loud — this is the only place
            # a surviving message is ever reported.
            _LOG.warning("Telegram refused to delete message %d: %s", message_id, error)
            return False
        return True

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
        self._drain_owed()
        self._callbacks.prune_for_message(self._chat_id, message_id)
        self._callbacks.bind_pending(self._chat_id, message_id)

    def _drain_owed(self) -> None:
        """Discard the tokens of every message an interstitial re-send could not retire.

        Called from both places a render finishes retiring — the ordinary edit and a
        re-send that is itself retiring. Living in only the first is how a deferred prune
        gets skipped exactly when two refusals arrive in a row.
        """
        for message_id in self._owed_prunes:
            self._callbacks.prune_for_message(self._chat_id, message_id)
        self._owed_prunes.clear()
