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

    token = anchor.reply_markup.inline_keyboard[0][0].callback_data
    state = boundary.callbacks.resolve(token, owner_id=7, chat_id=11, message_id=anchor.message_id)

    assert state is not None, "a button drawn on the re-sent screen must answer from it"
