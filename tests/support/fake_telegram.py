"""A private Telegram chat, modelled closely enough to count what is left in it.

Stage 2's claim is about the **chat**, not about any one call: after a journey the chat
holds one bot message and nothing the owner sent. A per-call spy cannot answer that — it
records what was asked for and forgets what survived — so this double keeps the messages
and lets a test read the transcript the owner would actually scroll.

The refusals are modelled too, because they are the ones the design turns on:

- editing a message that is gone raises `Message to edit not found`
- editing to identical content raises `Message is not modified` (the dead-button gotcha:
  Telegram makes a no-op an *error*)
- deleting a message that is gone raises `Message to delete not found`

A harness that quietly accepted those would let exactly the bugs this stage is about pass.

**What the content comparison can and cannot catch.** Every keyboarded screen embeds at
least one freshly minted token, so two consecutive renders of the *same* screen differ and
the no-op branch cannot fire for them — it is reachable only for the keyboardless screens
(an interstitial, an instruction). So this branch is not the coverage that protects the
dead-button gotcha; `edit_error` is. Set it to drive a refusal directly, the way
`tests/unit/adapters/telegram/test_live_view.py` does, rather than trying to provoke one
through content that random tokens will always make unequal.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from telegram.error import BadRequest

OWNER_USER_ID = 7
OWNER_CHAT_ID = 11


@dataclass
class Sent:
    """One message as it currently stands in the chat."""

    message_id: int
    author: str
    text: str
    reply_markup: object = None
    document: bytes | None = None
    filename: str | None = None
    protect_content: bool = False


class FakeChat:
    """The owner's private chat, and the only record of what they can see."""

    def __init__(self, chat_id: int = OWNER_CHAT_ID, owner_id: int = OWNER_USER_ID) -> None:
        self.chat_id = chat_id
        self.owner_id = owner_id
        self.messages: dict[int, Sent] = {}
        self.bot = FakeBot(self)
        self._next_id = 100

    def _add(self, author: str, text: str, **extra: object) -> Sent:
        message = Sent(self._next_id, author, text, **extra)
        self.messages[self._next_id] = message
        self._next_id += 1
        return message

    def _ordered(self) -> list[Sent]:
        return [self.messages[key] for key in sorted(self.messages)]

    @property
    def bot_messages(self) -> list[Sent]:
        return [message for message in self._ordered() if message.author == "bot"]

    @property
    def owner_messages(self) -> list[Sent]:
        return [message for message in self._ordered() if message.author == "owner"]

    def transcript(self) -> list[str]:
        """What the owner would scroll, in order — for a failure message worth reading."""
        return [f"{message.author}: {message.text}" for message in self._ordered()]

    def owner_sends(self, text: str) -> ChatMessage:
        """The owner types something: a command, a search term, a label."""
        return ChatMessage(self, self._add("owner", text))

    def message_update(self, text: str) -> SimpleNamespace:
        """An update carrying a message the owner just sent."""
        return self._update(effective_message=self.owner_sends(text))

    def press(self, token: str, *, on: int | None = None) -> SimpleNamespace:
        """An update carrying a button press on a bot message.

        Defaults to the newest bot message, which under one live view is the only one there
        is — a test that has to name the message is usually a test about a chat that grew a
        second screen.
        """
        if on is None:
            if not self.bot_messages:
                raise AssertionError("no bot message to press a button on")
            on = self.bot_messages[-1].message_id
        return self._update(callback_query=FakeCallbackQuery(self, token, on))

    def _update(self, **carrier: object) -> SimpleNamespace:
        source = carrier.get("callback_query") or carrier.get("effective_message")
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=self.owner_id),
            effective_chat=SimpleNamespace(id=self.chat_id, type="private"),
            effective_message=carrier.get("effective_message"),
            callback_query=carrier.get("callback_query"),
            get_bot=lambda: source.get_bot(),
        )


class ChatMessage:
    """A handle on one message, shaped the way a handler receives it."""

    def __init__(self, chat: FakeChat, record: Sent) -> None:
        self._chat = chat
        self._record = record

    @property
    def message_id(self) -> int:
        return self._record.message_id

    @property
    def text(self) -> str:
        return self._record.text

    def get_bot(self) -> FakeBot:
        return self._chat.bot

    async def reply_text(self, text: str | None = None, **kwargs: object) -> ChatMessage:
        """Send a *separate* bot message beside the live view.

        Kept because one thing genuinely needs it: a `ForceReply` cannot ride on an edit of
        a message carrying an inline keyboard, so the entry prompt is its own message. That
        message is then the caller's to discard once it has been answered.
        """
        body = text if text is not None else str(kwargs.get("text", ""))
        return ChatMessage(
            self._chat, self._chat._add("bot", body, reply_markup=kwargs.get("reply_markup"))
        )

    async def reply_document(self, **kwargs: object) -> ChatMessage:
        document = kwargs["document"]
        return ChatMessage(
            self._chat,
            self._chat._add(
                "bot",
                "",
                document=document.read(),
                filename=kwargs["filename"],
                protect_content=bool(kwargs.get("protect_content", False)),
            ),
        )


class FakeCallbackQuery:
    def __init__(self, chat: FakeChat, data: str, message_id: int) -> None:
        self.data = data
        self.answers: list[str | None] = []
        self.alerts: list[bool] = []
        self._chat = chat
        self._message_id = message_id

    @property
    def message(self) -> ChatMessage | None:
        record = self._chat.messages.get(self._message_id)
        return None if record is None else ChatMessage(self._chat, record)

    def get_bot(self) -> FakeBot:
        return self._chat.bot

    async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
        self.answers.append(text)
        self.alerts.append(show_alert)


class LoneMessageBot:
    """A bot for a double that models one message rather than a whole chat.

    For the tests that assert on what a handler *sent*, not on what the chat is left
    holding. A send and an edit both land in the owner's `replies`, because from such a
    test's point of view they are the same event — this screen was drawn in answer to this
    update — and which one Telegram performed depends only on whether an anchor existed.

    Use `FakeChat` instead whenever the claim is about the chat; only that one can count
    what survived.
    """

    def __init__(self, owner: object) -> None:
        self._owner = owner

    async def send_message(self, **kwargs: object) -> SimpleNamespace:
        kwargs.pop("chat_id", None)
        self._owner.replies.append(kwargs)
        return SimpleNamespace(message_id=self._owner.message_id)

    async def edit_message_text(self, **kwargs: object) -> None:
        kwargs.pop("chat_id", None)
        kwargs.pop("message_id", None)
        self._owner.replies.append(kwargs)

    async def delete_message(self, **kwargs: object) -> None:
        self._owner.deletions.append(int(kwargs["message_id"]))


class FakeBot:
    """Telegram's message surface, refusing what Telegram refuses."""

    def __init__(self, chat: FakeChat) -> None:
        self._chat = chat
        self.send_error: Exception | None = None
        """Force every send to fail — a rate limit, a 5xx, a dropped connection.

        Ordering rules only have consequences when something fails, so a harness that can
        only succeed cannot tell a safe order from an unsafe one.
        """
        self.edit_error: Exception | None = None
        """Force the next and every subsequent edit to be refused.

        The only way to reach the 48-hour `Message can't be edited` case, which no amount
        of driving the surface can produce: the harness has no clock and Telegram's window
        is not a property of the content.
        """

    def _require_chat(self, chat_id: int) -> None:
        if chat_id != self._chat.chat_id:
            raise AssertionError(f"addressed chat {chat_id}, which is not the owner's")

    async def send_message(self, *, chat_id: int, text: str, **kwargs: object) -> Sent:
        self._require_chat(chat_id)
        if self.send_error is not None:
            raise self.send_error
        return self._chat._add("bot", text, reply_markup=kwargs.get("reply_markup"))

    async def edit_message_text(
        self, *, chat_id: int, message_id: int, text: str, **kwargs: object
    ) -> None:
        self._require_chat(chat_id)
        if self.edit_error is not None:
            raise self.edit_error
        existing = self._chat.messages.get(message_id)
        if existing is None:
            raise BadRequest("Message to edit not found")
        if existing.author != "bot":
            raise BadRequest("Message can't be edited")
        markup = kwargs.get("reply_markup")
        if existing.text == text and existing.reply_markup == markup:
            raise BadRequest("Message is not modified: specified new message content")
        existing.text = text
        existing.reply_markup = markup

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self._require_chat(chat_id)
        if self._chat.messages.pop(message_id, None) is None:
            raise BadRequest("Message to delete not found")
