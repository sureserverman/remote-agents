from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fake_telegram import FakeChat
from stop_results import a_clean_stop
from telegram.error import BadRequest

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.inspection import inspect_capture
from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.adapters.telegram.stops import StopController
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


def test_telegram_action_audit_accepts_the_closed_adapter_surface() -> None:
    completed = subprocess.run(
        [sys.executable, "tests/architecture/check_telegram_actions.py"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert (
        "launch/resume/list/inspect/graceful/cleanup/force/create-project/navigation"
        in completed.stdout
    )


@pytest.mark.asyncio
async def test_fake_backend_primitives_cover_read_only_inspection_and_confirmed_stop() -> None:
    session = SessionId(UUID(int=1))
    inspection = inspect_capture(b"ready\n")
    callbacks = CallbackStateStore()
    stops = StopController(callbacks)
    token = stops.offer(session, ProfileId("claude"), SessionState.RUNNING, "graceful", 7, 11)
    callbacks.bind_pending(11, 1)

    assert inspection.text == "ready"
    assert token is not None
    claimed = stops.claim(token, 7, 11, 1)
    assert claimed is not None and claimed.action == "graceful"


def test_fake_journey_contract_covers_commands_recovery_and_oversized_inspection() -> None:
    """Keep the owner journey discoverable without requiring a live Telegram account."""
    owner_commands = ("/launch", "/sessions", "/help")
    expired = CallbackStateStore().resolve("missing", owner_id=7, chat_id=11, message_id=1)
    attachment = inspect_capture(("x" * 30).encode(), telegram_limit=20)

    assert owner_commands == ("/launch", "/sessions", "/help")
    assert expired is None, "expired callbacks recover to Home after acknowledgement"
    assert attachment.filename == "session-output.txt"
    assert attachment.attachment is not None
    assert "Back" != "Cancel"


@pytest.mark.asyncio
async def test_stop_controller_rechecks_and_dispatches_against_fakes() -> None:
    session = SessionId(UUID(int=2))
    record = SessionRecord(
        session,
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "Claude", "regular", 2),
        SessionState.RUNNING,
        datetime(2026, 7, 31, tzinfo=UTC),
    )

    class Service:
        def __init__(self) -> None:
            self.called = False

        async def graceful_stop(self, _command):
            self.called = True
            return a_clean_stop()

    callbacks = CallbackStateStore()
    stops = StopController(callbacks)
    token = stops.offer(session, ProfileId("claude"), SessionState.RUNNING, "graceful", 7, 11)
    callbacks.bind_pending(11, 1)
    assert token is not None
    request = stops.claim(token, 7, 11, 1)
    assert request is not None
    service = Service()
    assert (await stops.execute(request, service, record)).dispatched
    assert service.called


def _boundary(*records: SessionRecord) -> PrivateBotBoundary:
    """A boundary over a chat's worth of state, with no terminal behind it."""

    class _Launcher:
        async def list_sessions(self):
            return list(records)

        async def refresh_readiness(self) -> None:
            return None

    return PrivateBotBoundary(
        7,
        11,
        catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),),
        launcher=_Launcher(),
    )


def _button(message, label: str) -> str:
    """The callback token behind a button, found by its label rather than its position.

    Rows move as screens gain and lose actions, and an index that silently points at
    `Back` produces a test that passes by doing nothing.
    """
    for row in message.reply_markup.inline_keyboard:
        for button in row:
            if button.text == label:
                return button.callback_data
    raise AssertionError(f"no {label!r} button in {message.text!r}")


@pytest.mark.asyncio
async def test_commands_render_in_place_and_leave_the_chat_holding_one_screen() -> None:
    """Four commands used to mean four screens, each keeping working buttons since Stage 1.

    This is the property the owner actually asked for, and it is a claim about the chat
    rather than about any single call: whatever the handlers did, what is *left* is one
    bot message and nothing the owner typed.
    """
    chat = FakeChat()
    boundary = _boundary()
    handlers = {
        "/start": boundary.start,
        "/launch": boundary.launch_command,
        "/sessions": boundary.sessions_command,
        "/help": boundary.help_command,
    }

    for command, handler in handlers.items():
        await handler(chat.message_update(command), None)

    assert len(chat.bot_messages) == 1, chat.transcript()
    assert chat.owner_messages == [], "a command the bot has answered is not part of the chat"
    assert chat.bot_messages[0].text.startswith("<b>Remote agents</b>"), (
        "the surviving screen is the last one asked for, not the first one drawn"
    )


@pytest.mark.asyncio
async def test_each_command_redraws_the_same_message_rather_than_adding_one() -> None:
    """The identity of the screen is the point — one message id, redrawn four times."""
    chat = FakeChat()
    boundary = _boundary()

    await boundary.start(chat.message_update("/start"), None)
    anchor = chat.bot_messages[0].message_id
    for command, handler in (
        ("/launch", boundary.launch_command),
        ("/sessions", boundary.sessions_command),
        ("/help", boundary.help_command),
    ):
        await handler(chat.message_update(command), None)

    assert [message.message_id for message in chat.bot_messages] == [anchor]


@pytest.mark.asyncio
async def test_a_command_is_answered_before_it_is_taken_out_of_the_chat() -> None:
    """Render first, delete second: a failed render must not also swallow the command.

    Otherwise the owner sends `/sessions`, sees it vanish, and has nothing at all to read
    about why nothing happened.
    """
    chat = FakeChat()
    boundary = _boundary()

    async def _refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the screen could not be drawn")

    boundary.view.render = _refuse  # type: ignore[method-assign]
    update = chat.message_update("/sessions")

    with pytest.raises(RuntimeError):
        await boundary.sessions_command(update, None)

    assert [message.text for message in chat.owner_messages] == ["/sessions"]


@pytest.mark.asyncio
async def test_a_command_whose_anchor_is_too_old_to_edit_still_leaves_one_screen() -> None:
    """The two deletions in one handler, and the reason they are both legitimate.

    A live view older than Telegram's 48-hour edit window forces a re-send, so answering
    one command retires the old anchor *and* consumes the command. Both are in the allowed
    set — a superseded screen of ours, and an owner input already answered — and what the
    chat is left holding is still exactly one message.
    """
    chat = FakeChat()
    boundary = _boundary()
    await boundary.start(chat.message_update("/start"), None)
    stale = chat.bot_messages[0].message_id
    chat.bot.edit_error = BadRequest("Message can't be edited")

    await boundary.sessions_command(chat.message_update("/sessions"), None)

    assert len(chat.bot_messages) == 1, chat.transcript()
    assert chat.owner_messages == []
    assert chat.bot_messages[0].message_id != stale, "the view moved to a writable message"
    assert stale not in chat.messages, "the message it could not edit is not left behind"


@pytest.mark.asyncio
async def test_a_command_screens_buttons_work_after_its_anchor_was_re_sent() -> None:
    """The token bookkeeping under a re-send: the new screen's buttons must resolve.

    This is the Stage 1 failure class in the shape Task 2.3 could reintroduce — a token
    minted for a screen that then moved to a different message id.
    """
    chat = FakeChat()
    boundary = _boundary()
    await boundary.start(chat.message_update("/start"), None)
    chat.bot.edit_error = BadRequest("Message can't be edited")
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    chat.bot.edit_error = None
    anchor = chat.bot_messages[0]

    token = _button(anchor, "Launch")
    state = boundary.callbacks.resolve(token, owner_id=7, chat_id=11, message_id=anchor.message_id)

    assert state is not None, "a button drawn on the re-sent screen must answer from it"


@pytest.mark.asyncio
async def test_guided_entry_leaves_the_live_view_and_nothing_else() -> None:
    """A search costs the chat one prompt while it is open, and nothing once it is answered.

    Both halves matter. The prompt has to be its own message — a `ForceReply` cannot ride
    on an edit of a message carrying an inline keyboard — and it has to leave again, or the
    chat keeps an input box for a question already answered.
    """
    chat = FakeChat()
    boundary = _boundary()
    await boundary.start(chat.message_update("/start"), None)
    anchor = chat.bot_messages[0].message_id
    launch = _button(chat.bot_messages[0], "Launch")
    await boundary.callback(chat.press(launch), None)
    search = _button(chat.messages[anchor], "Search")

    await boundary.callback(chat.press(search), None)

    prompts = [message for message in chat.bot_messages if message.message_id != anchor]
    assert len(prompts) == 1, "the input box is one extra message, sent rather than edited in"
    assert prompts[0].reply_markup.input_field_placeholder == "Project name"
    assert chat.messages[anchor].text == "Reply below with a project name."

    await boundary.text(chat.message_update("Demo"), None)

    assert len(chat.bot_messages) == 1, chat.transcript()
    assert chat.owner_messages == [], "the answer is consumed, not kept"
    assert chat.bot_messages[0].message_id == anchor
    assert "Demo" in str(chat.messages[anchor].reply_markup.inline_keyboard[0][0].text)


@pytest.mark.asyncio
async def test_guided_entry_that_is_refused_re_asks_without_accumulating() -> None:
    """Three failed attempts must cost the chat exactly what one does."""
    chat = FakeChat()
    boundary = _boundary()
    await boundary.start(chat.message_update("/start"), None)
    anchor = chat.bot_messages[0].message_id
    launch = _button(chat.bot_messages[0], "Launch")
    await boundary.callback(chat.press(launch), None)
    search = _button(chat.messages[anchor], "Search")
    await boundary.callback(chat.press(search), None)

    for attempt in ("nothing-like-this", "still-nothing", "nope"):
        await boundary.text(chat.message_update(attempt), None)
        assert len(chat.bot_messages) == 2, chat.transcript()
        assert chat.owner_messages == [], chat.transcript()

    box = next(message for message in chat.bot_messages if message.message_id != anchor)
    assert "No projects found" in box.text
    # The placeholder is the only thing that carries *which* step is being re-asked: the
    # notice text is the same string whatever action produced it, so without this a wrong
    # action threaded into the retry would serve a session-label box for a project search
    # and every other assertion here would still pass.
    assert box.reply_markup.input_field_placeholder == "Project name"

    await boundary.text(chat.message_update("Demo"), None)

    assert len(chat.bot_messages) == 1, chat.transcript()
    assert chat.owner_messages == []


@pytest.mark.asyncio
async def test_guided_entry_cancelled_returns_home_and_takes_the_input_box_with_it() -> None:
    chat = FakeChat()
    boundary = _boundary()
    await boundary.start(chat.message_update("/start"), None)
    anchor = chat.bot_messages[0].message_id
    launch = _button(chat.bot_messages[0], "Launch")
    await boundary.callback(chat.press(launch), None)
    search = _button(chat.messages[anchor], "Search")
    await boundary.callback(chat.press(search), None)

    await boundary.text(chat.message_update("cancel"), None)

    assert len(chat.bot_messages) == 1, chat.transcript()
    assert chat.owner_messages == []
    assert chat.messages[anchor].text.startswith("<b>Remote agents</b>")


@pytest.mark.asyncio
async def test_a_re_ask_that_cannot_be_sent_leaves_the_owner_a_way_to_answer() -> None:
    """The reason the new box is sent before the old one is taken away.

    If Telegram refuses the send — a rate limit, a 5xx — clearing first would leave the
    owner with no input box, nothing they typed, and a step the service still believes is
    open: no way forward except a command. Asking first costs one duplicated box for the
    length of a call and nothing if it succeeds.
    """
    chat = FakeChat()
    boundary = _boundary()
    await boundary.start(chat.message_update("/start"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.bot_messages[0], "Launch")), None)
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Search")), None)
    box = next(message for message in chat.bot_messages if message.message_id != anchor)
    chat.bot.send_error = BadRequest("Too Many Requests")

    with pytest.raises(BadRequest):
        await boundary.text(chat.message_update("nothing-like-this"), None)

    assert box.message_id in chat.messages, "the owner still has an input box to answer"
    assert [message.text for message in chat.owner_messages] == ["nothing-like-this"], (
        "and still has what they typed, rather than losing both to a failed re-ask"
    )
    assert boundary._awaiting_text[(7, 11)].input_message_id == box.message_id
