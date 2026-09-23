"""The bot's Send message flow: from a session's screen, one message typed into its agent.

DEC-099. The owner taps Send message, replies in the input box, and the message goes to the relay:
sent now if the agent is idle, queued (one per session, newest wins) if it is busy or at a dialog,
refused with a reason otherwise. A queued message shows on the session's screen with a Cancel,
and its later delivery is announced. The relay itself is doubled here -- its rules are pinned in
`tests/unit/application/test_prompt_relay.py` -- so what this file pins is the surface.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from backends import SessionUseCaseDouble, backend_for
from fake_telegram import FakeChat

from remote_agents.adapters.telegram.presenters import unpadded
from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.message_relay import RelayOutcome, RelayResult
from remote_agents.ports.queued_prompts import QueuedPrompt
from remote_agents.ports.terminal import PromptReason

OWNER = 7
CHAT = 11
_SESSION = SessionId(UUID(int=1))


class _Launcher(SessionUseCaseDouble):
    def __init__(self, record: SessionRecord) -> None:
        self.record = record

    async def list_sessions(self):
        return [self.record]

    async def refresh_readiness(self) -> None:
        return None


def _record(profile: str = "claude", state: SessionState = SessionState.RUNNING):
    return SessionRecord(
        _SESSION,
        ProjectId("p" * 24),
        ProfileId(profile),
        SessionDisplayIdentity("Demo", "Claude", "regular", 1),
        state,
        datetime(2026, 9, 23, tzinfo=UTC),
    )


class _Relay:
    def __init__(self, *results: RelayResult) -> None:
        self.results = list(results)
        self.submitted: list[tuple[SessionId, str]] = []
        self.cancelled: list[SessionId] = []
        self.waiting: QueuedPrompt | None = None

    async def submit(self, session_id, text):
        self.submitted.append((session_id, text))
        result = self.results.pop(0) if self.results else RelayResult(RelayOutcome.SENT)
        if result.outcome is RelayOutcome.QUEUED:
            self.waiting = QueuedPrompt(str(session_id), text, datetime.now(UTC))
        return result

    async def retry(self, session_id):
        return None

    def cancel(self, session_id) -> bool:
        self.cancelled.append(session_id)
        had = self.waiting is not None
        self.waiting = None
        return had

    def pending(self, session_id):
        return self.waiting

    async def sweep(self) -> None:
        return None


def _boundary(relay=None, *, record=None, relayable=frozenset({"claude"})):
    return build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(sessions=_Launcher(record or _record())),
        profiles=(ProfileAvailability("claude", True, None),),
        message_relay=relay,
        relayable=relayable,
    )


def _labels(message) -> list[str]:
    markup = getattr(message, "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", ())
    return [unpadded(button.text) for row in rows for button in row]


def _token(message, label: str) -> str:
    for row in message.reply_markup.inline_keyboard:
        for button in row:
            if unpadded(button.text).endswith(label):
                return button.callback_data
    raise AssertionError(f"no {label!r} button in {_labels(message)}")


def _open_boxes(chat: FakeChat) -> list[object]:
    return [
        message
        for message in chat.messages.values()
        if type(getattr(message, "reply_markup", None)).__name__ == "ForceReply"
    ]


async def _detail(chat: FakeChat, boundary) -> int:
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_token(chat.messages[anchor], "Demo")), None)
    return anchor


async def _send(chat: FakeChat, boundary, text: str) -> int:
    anchor = await _detail(chat, boundary)
    await boundary.callback(chat.press(_token(chat.messages[anchor], "Send message")), None)
    await boundary.text(chat.message_update(text), None)
    return anchor


@pytest.mark.asyncio
async def test_a_running_relayable_session_offers_send_message() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    anchor = await _detail(chat, _boundary(_Relay()))

    assert any(label.endswith("Send message") for label in _labels(chat.messages[anchor]))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wiring",
    [
        {"relay": None},
        {"relayable": frozenset()},
        {"record": _record(state=SessionState.UNTRUSTED)},
    ],
    ids=["no-relay", "agent-declares-no-composer", "not-running"],
)
async def test_send_message_is_not_offered_where_it_cannot_work(wiring) -> None:
    relay = wiring.pop("relay", _Relay())
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    anchor = await _detail(chat, _boundary(relay, **wiring))

    assert not any(label.endswith("Send message") for label in _labels(chat.messages[anchor]))


@pytest.mark.asyncio
async def test_tapping_send_message_opens_the_input_box_as_its_own_message() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary(_Relay())
    anchor = await _detail(chat, boundary)

    await boundary.callback(chat.press(_token(chat.messages[anchor], "Send message")), None)

    boxes = _open_boxes(chat)
    assert len(boxes) == 1 and boxes[0].message_id != anchor


@pytest.mark.asyncio
async def test_the_reply_reaches_the_relay_and_the_box_is_removed() -> None:
    relay = _Relay()
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)

    anchor = await _send(chat, _boundary(relay), "Please run the tests")

    assert relay.submitted == [(_SESSION, "Please run the tests")]
    assert _open_boxes(chat) == []
    assert "Sent" in chat.messages[anchor].text


@pytest.mark.asyncio
async def test_a_reply_from_anyone_but_the_owner_is_dropped() -> None:
    relay = _Relay()
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary(relay)
    anchor = await _detail(chat, boundary)
    await boundary.callback(chat.press(_token(chat.messages[anchor], "Send message")), None)

    stranger = chat.message_update("rm everything")
    stranger.effective_user.id = OWNER + 1
    await boundary.text(stranger, None)

    assert relay.submitted == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "words"),
    [
        (RelayResult(RelayOutcome.SENT), "Sent"),
        (RelayResult(RelayOutcome.REFUSED, PromptReason.SHELL), "Not sent"),
        (RelayResult(RelayOutcome.REFUSED, PromptReason.NOT_RUNNING), "isn't running"),
        (RelayResult(RelayOutcome.UNCONFIRMED, PromptReason.DRAFT_NOT_SEEN), "not confirmed"),
        (RelayResult(RelayOutcome.QUEUED, PromptReason.BUSY), "Queued"),
    ],
    ids=["sent", "refused", "not-running", "unconfirmed", "queued"],
)
async def test_each_outcome_is_said_in_its_own_words(result, words) -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)

    anchor = await _send(chat, _boundary(_Relay(result)), "hello")

    assert words in chat.messages[anchor].text, chat.messages[anchor].text


@pytest.mark.asyncio
async def test_a_queued_reply_carries_a_cancel_that_works() -> None:
    relay = _Relay(RelayResult(RelayOutcome.QUEUED, PromptReason.BUSY))
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary(relay)
    anchor = await _send(chat, boundary, "hello")

    await boundary.callback(
        chat.press(_token(chat.messages[anchor], "Cancel queued message")), None
    )

    assert relay.cancelled == [_SESSION]
    assert relay.waiting is None


@pytest.mark.asyncio
async def test_the_session_screen_shows_the_waiting_message() -> None:
    relay = _Relay()
    relay.waiting = QueuedPrompt(str(_SESSION), "Run the migration\nthen report", datetime.now(UTC))
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)

    anchor = await _detail(chat, _boundary(relay))

    text = chat.messages[anchor].text
    assert "Run the migration" in text and "then report" not in text
    assert any(label.endswith("Cancel queued message") for label in _labels(chat.messages[anchor]))


@pytest.mark.asyncio
async def test_a_second_message_replacing_the_waiting_one_says_so() -> None:
    relay = _Relay(RelayResult(RelayOutcome.QUEUED, PromptReason.BUSY, replaced=True))
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)

    anchor = await _send(chat, _boundary(relay), "newer")

    assert "replaced" in chat.messages[anchor].text


@pytest.mark.asyncio
async def test_a_delivered_waiting_message_is_announced() -> None:
    relay = _Relay()
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary(relay)
    boundary.attach_bot(chat.bot)
    await _detail(chat, boundary)
    before = len(chat.bot_messages)

    await boundary.announce_relayed(str(_SESSION), RelayResult(RelayOutcome.SENT))

    assert len(chat.bot_messages) == before + 1
    assert "Sent queued message" in chat.bot_messages[-1].text


@pytest.mark.asyncio
async def test_a_message_still_waiting_after_a_retry_is_not_news() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary(_Relay())
    boundary.attach_bot(chat.bot)
    before = len(chat.bot_messages)

    await boundary.announce_relayed(
        str(_SESSION), RelayResult(RelayOutcome.QUEUED, PromptReason.BUSY)
    )

    assert len(chat.bot_messages) == before


@pytest.mark.asyncio
async def test_a_relay_that_raises_closes_the_step_and_says_to_look() -> None:
    """Raising mid-step would leave the box open and every later reply raising again."""

    class _Broken(_Relay):
        async def submit(self, session_id, text):
            raise RuntimeError("database is locked")

    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)

    anchor = await _send(chat, _boundary(_Broken()), "hello")

    assert _open_boxes(chat) == []
    assert "Check the session" in chat.messages[anchor].text


@pytest.mark.asyncio
async def test_an_unreadable_queue_costs_the_queued_line_not_the_session_screen() -> None:
    class _Unreadable(_Relay):
        def pending(self, session_id):
            raise RuntimeError("database is locked")

    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)

    anchor = await _detail(chat, _boundary(_Unreadable()))

    labels = _labels(chat.messages[anchor])
    assert any(label.endswith("Send message") for label in labels)
    assert not any(label.endswith("Cancel queued message") for label in labels)


@pytest.mark.asyncio
async def test_a_waiting_message_on_a_session_that_stopped_running_keeps_only_its_cancel() -> None:
    relay = _Relay()
    relay.waiting = QueuedPrompt(str(_SESSION), "later", datetime.now(UTC))
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)

    anchor = await _detail(
        chat, _boundary(relay, record=_record(state=SessionState.STOP_REQUESTED))
    )

    labels = _labels(chat.messages[anchor])
    assert any(label.endswith("Cancel queued message") for label in labels)
    assert not any(label.endswith("Send message") for label in labels)


@pytest.mark.asyncio
async def test_an_unconfirmed_reply_never_opens_with_sent() -> None:
    """`Enter` may never have been pressed; "Sent" would contradict the reason beside it."""
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    relay = _Relay(RelayResult(RelayOutcome.UNCONFIRMED, PromptReason.DRAFT_NOT_SEEN))

    anchor = await _send(chat, _boundary(relay), "hello")

    text = chat.messages[anchor].text
    assert not text.startswith("Sent") and "may still be in the agent's input" in text


@pytest.mark.asyncio
async def test_cancelling_a_message_already_being_typed_says_it_may_still_arrive() -> None:
    relay = _Relay()
    relay.waiting = QueuedPrompt(
        str(_SESSION), "hello", datetime.now(UTC), claimed_at=datetime.now(UTC)
    )
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary(relay)
    anchor = await _detail(chat, boundary)

    await boundary.callback(
        chat.press(_token(chat.messages[anchor], "Cancel queued message")), None
    )

    assert relay.cancelled == [_SESSION]
    assert "may still arrive" in chat.messages[anchor].text


@pytest.mark.asyncio
async def test_cancelling_a_message_not_yet_being_typed_says_cancelled() -> None:
    relay = _Relay()
    relay.waiting = QueuedPrompt(str(_SESSION), "hello", datetime.now(UTC))
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary(relay)
    anchor = await _detail(chat, boundary)

    await boundary.callback(
        chat.press(_token(chat.messages[anchor], "Cancel queued message")), None
    )

    assert "Queued message cancelled." in chat.messages[anchor].text


@pytest.mark.asyncio
async def test_a_delivery_that_overtook_a_cancel_says_so() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary(_Relay())
    boundary.attach_bot(chat.bot)

    await boundary.announce_relayed(str(_SESSION), RelayResult(RelayOutcome.SENT, overtaken=True))

    assert "already being typed in when you cancelled" in chat.bot_messages[-1].text


@pytest.mark.parametrize("reason", list(PromptReason))
def test_every_reason_has_words_for_the_owner(reason) -> None:
    from remote_agents.adapters.telegram.service import _RELAY_REASON_WORDS

    assert _RELAY_REASON_WORDS.get(reason), f"{reason} would reach the chat with no words"


def test_nothing_in_the_flow_puts_the_word_the_scanner_forbids_in_the_adapter() -> None:
    """DEC-075: the action is `message` in this package; `check_telegram_actions` enforces it."""
    import importlib.util
    import pathlib

    script = (
        pathlib.Path(__file__).resolve().parents[4] / "tests/architecture/check_telegram_actions.py"
    )
    spec = importlib.util.spec_from_file_location("check_telegram_actions", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main() == 0
