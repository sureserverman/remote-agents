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

There is deliberately no `reply_text`/`reply_document` on a message here. Every outbound
message now goes through `LiveView`, which addresses the chat rather than replying to
whatever arrived, so a double offering the reply form would offer a route production no
longer has.

**What the content comparison can and cannot catch.** Every keyboarded screen embeds at
least one freshly minted token, so two consecutive renders of the *same* screen differ and
the no-op branch cannot fire for them — it is reachable only for the keyboardless screens
(an interstitial, an instruction). So this branch is not the coverage that protects the
dead-button gotcha; `edit_error` is. Set it to drive a refusal directly, the way
`tests/unit/adapters/telegram/test_live_view.py` does, rather than trying to provoke one
through content that random tokens will always make unequal.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from types import SimpleNamespace

from telegram import Bot
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


def reject_arguments_the_real_bot_would_refuse(method: str, kwargs: dict) -> None:
    """Fail a fake call that the real `telegram.Bot` method would not accept.

    Every bot double in this suite took `**kwargs` and recorded whatever it was handed, which
    meant a call could be *wrong at the API boundary* and green in every test. On 2026-09-04
    that shipped: the pairing-code reply carried `protect_content` -- a `sendMessage`
    parameter that `editMessageText` does not have -- through the live view's edit path, so
    every press raised `TypeError` in production and no code ever reached the owner. Nothing
    here could see it, because the fake accepted the argument the real one rejects.

    Checked against `inspect.signature` of the actual method rather than a hand-written list,
    so this keeps working when python-telegram-bot adds or removes a parameter, and covers
    every argument rather than the one that happened to bite. Verified against the installed
    version: `send_message` and `send_document` accept `protect_content`, `edit_message_text`
    does not, and none of the three declares `**kwargs`.
    """
    signature = inspect.signature(getattr(Bot, method))
    parameters = signature.parameters.values()
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return
    unexpected = sorted(set(kwargs) - {parameter.name for parameter in parameters})
    if unexpected:
        raise TypeError(
            f"ExtBot.{method}() got an unexpected keyword argument {unexpected[0]!r} -- "
            "the real bot would raise this too"
        )


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

        By default the press comes from the message actually **carrying** that button, which
        is what a thumb can do and nothing else is. Defaulting to the newest bot message
        instead looks equivalent right up until the chat holds a second one — a captured
        document, an input box — and then every press silently arrives from the wrong id and
        resolves to nothing, which reads exactly like the code being broken.

        Pass `on` to press a token from somewhere it was never drawn; that is a real case
        worth testing, but it should have to be asked for.
        """
        if on is None:
            on = self._carrier_of(token)
        return self._update(callback_query=FakeCallbackQuery(self, token, on))

    def _carrier_of(self, token: str) -> int:
        for message in self.bot_messages:
            keyboard = getattr(message.reply_markup, "inline_keyboard", ())
            if any(button.callback_data == token for row in keyboard for button in row):
                return message.message_id
        if not self.bot_messages:
            raise AssertionError("no bot message to press a button on")
        # A token no live keyboard carries — a stale one, or one never issued. The newest
        # screen is where a thumb would have found it.
        return self.bot_messages[-1].message_id

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
        reject_arguments_the_real_bot_would_refuse("send_message", kwargs)
        kwargs.pop("chat_id", None)
        self._owner.replies.append(kwargs)
        return SimpleNamespace(message_id=self._owner.message_id)

    async def edit_message_text(self, **kwargs: object) -> None:
        reject_arguments_the_real_bot_would_refuse("edit_message_text", kwargs)
        kwargs.pop("chat_id", None)
        kwargs.pop("message_id", None)
        self._owner.replies.append(kwargs)

    async def delete_message(self, **kwargs: object) -> None:
        self._owner.deletions.append(int(kwargs["message_id"]))

    async def send_document(self, **kwargs: object) -> SimpleNamespace:
        self._owner.documents.append(
            {
                "document": kwargs["document"].read(),
                "filename": kwargs["filename"],
                "protect_content": kwargs.get("protect_content", False),
            }
        )
        return SimpleNamespace(message_id=self._owner.message_id)


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
        reject_arguments_the_real_bot_would_refuse("send_message", kwargs)
        self._require_chat(chat_id)
        if self.send_error is not None:
            raise self.send_error
        # `protect_content` is recorded rather than dropped: it is the difference between a
        # secret the owner's other clients can save and one they cannot, so a test asserting
        # it needs somewhere to read it from.
        return self._chat._add(
            "bot",
            text,
            reply_markup=kwargs.get("reply_markup"),
            protect_content=bool(kwargs.get("protect_content", False)),
        )

    async def edit_message_text(
        self, *, chat_id: int, message_id: int, text: str, **kwargs: object
    ) -> None:
        reject_arguments_the_real_bot_would_refuse("edit_message_text", kwargs)
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

    async def edit_message_reply_markup(
        self, *, chat_id: int, message_id: int, **kwargs: object
    ) -> None:
        """Attach or replace a message's keyboard without touching its text.

        Modelled because a notification is sent before its button exists: the token is bound
        to the message the send answered with, so the keyboard can only arrive in a second
        call. Refuses a message that is gone, exactly as `edit_message_text` does — a harness
        that accepted an edit to nothing would let a notifier addressing the wrong id pass.
        """
        self._require_chat(chat_id)
        existing = self._chat.messages.get(message_id)
        if existing is None:
            raise BadRequest("Message to edit not found")
        existing.reply_markup = kwargs.get("reply_markup")

    async def send_document(self, *, chat_id: int, **kwargs: object) -> Sent:
        self._require_chat(chat_id)
        if self.send_error is not None:
            raise self.send_error
        return self._chat._add(
            "bot",
            "",
            document=kwargs["document"].read(),
            filename=kwargs["filename"],
            protect_content=bool(kwargs.get("protect_content", False)),
        )

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self._require_chat(chat_id)
        if self._chat.messages.pop(message_id, None) is None:
            raise BadRequest("Message to delete not found")
