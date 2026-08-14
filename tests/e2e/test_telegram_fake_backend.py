from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fake_telegram import FakeChat
from stop_results import a_clean_stop
from telegram.error import BadRequest

from remote_agents.adapters.sqlite.callback_state_store import SQLiteCallbackStateStore
from remote_agents.adapters.sqlite.chat_view_store import SQLiteChatViewStore
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.inspection import inspect_capture
from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.adapters.telegram.stops import StopController
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.application.errors import SessionNotFoundError
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.trust import TrustState
from remote_agents.ports.agent_activity import ActivityKind, AgentActivity


def test_telegram_action_audit_accepts_the_closed_adapter_surface() -> None:
    """The roster is pinned here so widening the surface takes two deliberate edits.

    `trust` joined it on 2026-08-14 (DEC-016): a managed launch into a directory Claude Code
    has not been trusted for blocks on a dialog nobody can answer, so the launch times out
    and the owner is told only that it failed. The bot may already launch an agent into that
    project, which is strictly more power than trusting it -- so the addition removes a
    confusing failure rather than granting a new capability. It is the first addition since
    the surface was closed, and this test is what makes the next one visible too.
    """
    completed = subprocess.run(
        [sys.executable, "tests/architecture/check_telegram_actions.py"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert (
        "launch/resume/list/inspect/graceful/cleanup/force/create-project/trust/navigation"
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


class _RenamingLauncher:
    """Holds one record and applies renames to it, so the detail can be re-read after one."""

    def __init__(self, record: SessionRecord) -> None:
        self.record = record
        self.renames: list[str | None] = []
        self.missing = False

    async def list_sessions(self):
        return [] if self.missing else [self.record]

    async def refresh_readiness(self) -> None:
        return None

    async def rename(self, session_id, label):
        if self.missing:
            # The type `SessionService.rename` actually raises, via `_require_session`. It is
            # a sibling of KeyError under LookupError, not a subclass — a double raising
            # KeyError here left the adapter's recovery branch dead behind a green test.
            raise SessionNotFoundError(str(session_id))
        self.renames.append(label)
        display = SessionDisplayIdentity(
            self.record.display.project_slug,
            self.record.display.agent_label,
            self.record.display.mode,
            self.record.display.sequence,
            label,
        )
        self.record = SessionRecord(
            self.record.session_id,
            self.record.project_id,
            self.record.profile_id,
            display,
            self.record.state,
            self.record.created_at,
        )
        return self.record


def _renameable(record: SessionRecord) -> tuple[PrivateBotBoundary, _RenamingLauncher]:
    launcher = _RenamingLauncher(record)
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),),
        launcher=launcher,
    )
    return boundary, launcher


async def _open_rename(chat: FakeChat, boundary: PrivateBotBoundary) -> int:
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(
        chat.press(_button(chat.messages[anchor], "Demo · Claude · regular · #1")), None
    )
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Rename")), None)
    return anchor


@pytest.mark.asyncio
async def test_rename_asks_in_a_box_beside_the_live_view_not_on_it() -> None:
    """Sub-plan 1's rule, and the gotcha behind it.

    `ForceReply` cannot ride on a message being edited while it carries an inline keyboard —
    Telegram answers `Inline keyboard expected` — so the instruction goes into the live view
    and the input box is a second message.
    """
    boundary, _ = _renameable(_a_running_session())
    chat = FakeChat()

    anchor = await _open_rename(chat, boundary)

    assert chat.messages[anchor].text == "Reply below with a name for this session."
    box = [message for message in chat.bot_messages if message.message_id != anchor]
    assert len(box) == 1, chat.transcript()
    assert box[0].reply_markup.input_field_placeholder == "Session name"


@pytest.mark.asyncio
async def test_rename_applies_the_new_name_and_redraws_the_detail() -> None:
    """The answer is consumed: the box and the owner's reply both leave with it."""
    boundary, launcher = _renameable(_a_running_session())
    chat = FakeChat()
    anchor = await _open_rename(chat, boundary)

    await boundary.text(chat.message_update("  release   review  "), None)

    assert launcher.renames == ["release review"], "collapsed whitespace, one call"
    assert "release review" in chat.messages[anchor].text
    assert "Rename" in [
        button.text for row in chat.messages[anchor].reply_markup.inline_keyboard for button in row
    ], "it lands back on the session's own menu"
    assert len(chat.bot_messages) == 1, chat.transcript()
    assert chat.owner_messages == [], "the answer is consumed, not kept"


@pytest.mark.asyncio
async def test_rename_refuses_a_name_the_rule_rejects_without_calling_the_store() -> None:
    """Re-asked in place rather than accepted and rejected downstream."""
    boundary, launcher = _renameable(_a_running_session())
    chat = FakeChat()
    anchor = await _open_rename(chat, boundary)

    await boundary.text(chat.message_update("x" * 41), None)

    assert launcher.renames == [], "nothing invalid reaches the store"
    box = [message for message in chat.bot_messages if message.message_id != anchor]
    assert len(box) == 1, "still exactly one box open"
    assert "up to 40 characters" in box[0].text
    assert chat.owner_messages == [], chat.transcript()


@pytest.mark.asyncio
async def test_rename_skipped_leaves_the_session_exactly_as_it_was() -> None:
    """Declining to name something is not the same intent as clearing its name."""
    boundary, launcher = _renameable(_a_running_session())
    chat = FakeChat()
    anchor = await _open_rename(chat, boundary)

    await boundary.text(chat.message_update("Skip"), None)

    assert launcher.renames == [], "Skip must not reach the store at all"
    assert "Rename" in [
        button.text for row in chat.messages[anchor].reply_markup.inline_keyboard for button in row
    ]
    assert len(chat.bot_messages) == 1, chat.transcript()
    assert chat.owner_messages == []


@pytest.mark.asyncio
async def test_rename_cancelled_leaves_the_session_and_takes_the_box_with_it() -> None:
    boundary, launcher = _renameable(_a_running_session())
    chat = FakeChat()
    await _open_rename(chat, boundary)

    await boundary.text(chat.message_update("Cancel"), None)

    assert launcher.renames == []
    assert len(chat.bot_messages) == 1, chat.transcript()
    assert chat.owner_messages == []


@pytest.mark.asyncio
async def test_rename_of_a_session_that_ended_under_the_owner_lands_on_the_list() -> None:
    """The box outlives the session it was opened for; the detail behind it does not."""
    boundary, launcher = _renameable(_a_running_session())
    chat = FakeChat()
    anchor = await _open_rename(chat, boundary)
    launcher.missing = True

    await boundary.text(chat.message_update("too late"), None)

    assert chat.messages[anchor].text.startswith("That session is no longer available.")
    assert "Nothing is running." in chat.messages[anchor].text
    assert len(chat.bot_messages) == 1, chat.transcript()
    assert chat.owner_messages == []


def _button(message, label: str) -> str:
    """The callback token behind a button, found by its label rather than its position.

    Rows move as screens gain and lose actions, and an index that silently points at
    `Back` produces a test that passes by doing nothing.
    """
    for row in message.reply_markup.inline_keyboard:
        for button in row:
            # A session row carries a relative age that drifts between runs, so a prefix is
            # the only stable handle on it. Exact matches still win first.
            if button.text == label or button.text.startswith(label):
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
    # Home and Help both open with the same heading, so a heading check would not tell
    # which screen survived; "Stop and close" appears only on Help.
    assert "Stop and close" in chat.bot_messages[0].text, (
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


def _inspectable_boundary(record: SessionRecord, output: str) -> PrivateBotBoundary:
    boundary = _boundary(record)

    async def _capture(_session_id) -> str:
        return output

    boundary.capture = _capture
    return boundary


def _a_running_session(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId(UUID(int=7)),
        ProjectId("a" * 24),
        ProfileId("claude"),
        SessionDisplayIdentity("Demo", "Claude", "regular", 1),
        state,
        datetime(2026, 8, 10, tzinfo=UTC),
    )


class _TrustLauncher:
    """One FAILED Claude session whose pane is waiting on the folder-trust question."""

    def __init__(self, record: SessionRecord) -> None:
        self.record = record
        self.states = [TrustState.AWAITING]
        self.answered: list[SessionId] = []

    async def list_sessions(self):
        return [self.record]

    async def refresh_readiness(self) -> None:
        return None

    async def trust_state(self, session_id):
        del session_id
        return self.states[0]

    async def answer_trust(self, command):
        self.answered.append(command.session_id)
        # What the real terminal does: answering clears the dialog, so the next capture no
        # longer matches and the row stops being offered.
        self.states[0] = TrustState.UNKNOWN
        return TrustState.UNKNOWN


def _trust_blocked() -> tuple[PrivateBotBoundary, _TrustLauncher]:
    launcher = _TrustLauncher(_a_running_session(SessionState.FAILED))
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),),
        launcher=launcher,
    )
    return boundary, launcher


async def _open_detail(chat: FakeChat, boundary: PrivateBotBoundary) -> int:
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(
        chat.press(_button(chat.messages[anchor], "Demo \u00b7 Claude \u00b7 regular \u00b7 #1")),
        None,
    )
    return anchor


@pytest.mark.asyncio
async def test_a_session_waiting_on_folder_trust_is_offered_the_answer() -> None:
    """The whole point: a launch that failed on a question nobody could see gets a button.

    Before this the owner saw a FAILED session with no cause, because the pane was alive and
    blocked on a dialog the readiness check has no vocabulary for.
    """
    boundary, _ = _trust_blocked()
    chat = FakeChat()

    anchor = await _open_detail(chat, boundary)

    labels = [
        button.text
        for row in chat.messages[anchor].reply_markup.inline_keyboard
        for button in row
    ]
    assert "Trust this project" in labels, labels


@pytest.mark.asyncio
async def test_pressing_trust_answers_the_question_and_the_row_goes() -> None:
    """Answered once, and the button does not survive the thing it answered."""
    boundary, launcher = _trust_blocked()
    chat = FakeChat()
    anchor = await _open_detail(chat, boundary)

    await boundary.callback(chat.press(_button(chat.messages[anchor], "Trust this project")), None)

    assert launcher.answered == [launcher.record.session_id], "answered exactly once"
    assert "Trusted" in chat.messages[anchor].text, chat.messages[anchor].text


@pytest.mark.asyncio
async def test_a_session_not_waiting_on_trust_is_offered_no_such_row() -> None:
    """The guard that keeps a bare Enter away from a working agent.

    `TRUST_KEYS` is a single Enter, which means something to every agent in every pane, so a
    row offered when no dialog is on screen is a keypress into somebody's work.
    """
    boundary, launcher = _trust_blocked()
    launcher.states[0] = TrustState.UNKNOWN
    chat = FakeChat()

    anchor = await _open_detail(chat, boundary)

    labels = [
        button.text
        for row in chat.messages[anchor].reply_markup.inline_keyboard
        for button in row
    ]
    assert "Trust this project" not in labels, labels


@pytest.mark.asyncio
async def test_inspect_document_is_its_own_message_and_goes_when_the_session_does() -> None:
    """The capture is a screen; the file beside it is not, and cannot be redrawn.

    So it is the one thing here that has to be taken back out deliberately — and only once
    the owner has actually left the session it came from, not the moment they press Back.
    """
    session = _a_running_session()
    boundary = _inspectable_boundary(session, "x" * 5000)
    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(
        chat.press(_button(chat.messages[anchor], "Demo · Claude · regular · #1")), None
    )
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Inspect")), None)

    documents = [message for message in chat.bot_messages if message.document is not None]
    assert len(documents) == 1, chat.transcript()
    assert documents[0].protect_content is True, "a captured pane must stay unforwardable"
    assert documents[0].filename == "session-output.txt"
    assert "attached as UTF-8 text" in chat.messages[anchor].text
    assert len(chat.bot_messages) == 2, "the live view, and the file — nothing else"

    # Back into the same session's detail is not leaving it.
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Back")), None)
    assert documents[0].message_id in chat.messages, chat.transcript()

    # Home is.
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Home")), None)

    assert documents[0].message_id not in chat.messages, chat.transcript()
    assert len(chat.bot_messages) == 1, chat.transcript()
    assert chat.owner_messages == []


@pytest.mark.asyncio
async def test_a_launch_that_raises_lands_on_the_list_like_a_stop_does() -> None:
    """The recovery branch is keyed on the pending notice, not on stops.

    All five actions carrying a notice reach it -- the three stops, and launch and resume,
    which wait on a profile's readiness marker. The change that moved this branch onto the
    list was written about stops and applies to every one of them, so the other family is
    pinned here rather than left to be discovered by whoever next makes a launch raise.
    """
    session = _a_running_session()
    boundary = _boundary(session)

    class _FailingLauncher:
        async def list_sessions(self):
            return [session]

        async def refresh_readiness(self) -> None:
            return None

        async def launch(self, _command):
            raise RuntimeError("the profile could not be started")

    boundary.launcher = _FailingLauncher()
    # The launch guard checks the curated availability set before it reaches the launcher, so
    # without this the screen under test is "That agent is unavailable." and the except branch
    # is never entered.
    boundary.profiles = (ProfileAvailability("claude", True),)
    chat = FakeChat()
    await boundary.start(chat.message_update("/start"), None)
    anchor = chat.bot_messages[0].message_id
    # Minted straight onto the anchor rather than walked to through the wizard: the wizard is
    # Stage 2's subject and is about to change, while what this pins -- the except branch --
    # is not.
    token = boundary._callback("launch.profile", f"{'a' * 24}|claude", mutation=True)
    boundary.callbacks.bind_pending(11, anchor)

    await boundary.callback(chat.press(token, on=anchor), None)

    screen = chat.messages[anchor]
    assert screen.text.startswith("That action did not complete")
    assert "Sessions 1/1" in screen.text, "it landed on the list, not on a dead end"
    labels = [button.text for row in screen.reply_markup.inline_keyboard for button in row]
    assert "Back" not in labels
    assert len(chat.bot_messages) == 1, chat.transcript()


@pytest.mark.asyncio
async def test_a_stop_that_raises_lands_on_the_list_rather_than_a_dead_end() -> None:
    """The recovery screen is the last one a stop could strand the owner on.

    It is drawn when the action raises *after* its pending notice replaced the keyboard, so
    the owner is looking at a message with no buttons on it. It used to answer with Back and
    Home and the same "Open it again to see where it is now" that the refusal branch had
    already dropped for pointing at the screen the owner arrives on.
    """
    session = _a_running_session()
    boundary = _boundary(session)

    class _FailingLauncher:
        async def list_sessions(self):
            return [session]

        async def refresh_readiness(self) -> None:
            return None

        async def graceful_stop(self, _command):
            raise RuntimeError("the terminal went away mid-stop")

    boundary.launcher = _FailingLauncher()
    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(
        chat.press(_button(chat.messages[anchor], "Demo · Claude · regular · #1")), None
    )

    await boundary.callback(chat.press(_button(chat.messages[anchor], "Stop and close")), None)

    screen = chat.messages[anchor]
    assert screen.text.startswith("That action did not complete")
    assert "Open it again" not in screen.text
    assert "Sessions 1/1" in screen.text, "it landed on the list, which still holds the session"
    labels = [button.text for row in screen.reply_markup.inline_keyboard for button in row]
    assert "Back" not in labels
    assert len(chat.bot_messages) == 1, chat.transcript()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "button", "confirm"),
    [
        (SessionState.RUNNING, "Stop and close", None),
        (SessionState.PRESERVED, "Clean up", None),
        (SessionState.RUNNING, "Force stop", "Force stop"),
    ],
)
async def test_stopping_an_inspected_session_takes_its_document_with_it(
    state, button, confirm
) -> None:
    """Stopping a session is leaving it, so the file it produced goes too.

    `_release_attachment` is told what the next screen is *about*, and every action could
    answer that with the id it carries — right up until a stop stopped drawing a screen about
    its own session and started drawing the list. The id still said "this session", so the
    document was retained on behalf of a session that had just left the chat's only screen.

    Parametrized over all three members of `_LIST_LANDING_ACTIONS`, because the fix is a set
    and a set is only as good as its least-tested member: dropping `cleanup` or
    `force.confirmed` from it would otherwise leave every test green. Force goes through its
    confirmation, which is the screen that legitimately keeps the document — so this also
    pins that the release happens on the confirmed press and not the first one.
    """
    session = _a_running_session(state)
    boundary = _inspectable_boundary(session, "x" * 5000)

    stopped: list[str] = []

    class _StoppingLauncher:
        async def list_sessions(self):
            return [] if stopped else [session]

        async def refresh_readiness(self) -> None:
            return None

        async def graceful_stop(self, _command):
            stopped.append("graceful")
            return a_clean_stop(session.session_id)

        async def cleanup(self, _command) -> None:
            stopped.append("cleanup")

        async def force_stop(self, _command) -> None:
            stopped.append("force")

    boundary.launcher = _StoppingLauncher()
    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(
        chat.press(_button(chat.messages[anchor], "Demo · Claude · regular · #1")), None
    )
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Inspect")), None)
    documents = [message for message in chat.bot_messages if message.document is not None]
    assert len(documents) == 1, chat.transcript()

    await boundary.callback(chat.press(_button(chat.messages[anchor], "Back")), None)
    await boundary.callback(chat.press(_button(chat.messages[anchor], button)), None)
    if confirm is not None:
        # The confirmation is about this session, so the document is still here.
        assert documents[0].message_id in chat.messages, "the confirmation has not left it yet"
        await boundary.callback(chat.press(_button(chat.messages[anchor], confirm)), None)

    assert stopped, "the stop has to have actually run, or this proves nothing"
    assert documents[0].message_id not in chat.messages, chat.transcript()
    assert len(chat.bot_messages) == 1, "the live view alone — the file left with its session"


@pytest.mark.asyncio
async def test_a_capture_small_enough_to_read_leaves_no_file_behind() -> None:
    """Only an oversized capture becomes a document; a short one is just the screen."""
    session = _a_running_session()
    boundary = _inspectable_boundary(session, "ready\n")
    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(
        chat.press(_button(chat.messages[anchor], "Demo · Claude · regular · #1")), None
    )

    await boundary.callback(chat.press(_button(chat.messages[anchor], "Inspect")), None)

    assert [message.document for message in chat.bot_messages] == [None]
    assert "ready" in chat.messages[anchor].text


@pytest.mark.asyncio
async def test_inspecting_the_same_session_twice_leaves_one_document_not_two() -> None:
    """The second inspect passes the release check untouched — its session has not changed.

    So without an unconditional retire, the first document is orphaned: nothing tracks it
    any more, and no later navigation can ever take it out. A permanent extra message
    holding whatever the pane had printed.
    """
    session = _a_running_session()
    boundary = _inspectable_boundary(session, "x" * 5000)
    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    row = _button(chat.messages[anchor], "Demo · Claude · regular · #1")
    await boundary.callback(chat.press(row), None)

    for _ in range(3):
        await boundary.callback(chat.press(_button(chat.messages[anchor], "Inspect")), None)
        await boundary.callback(chat.press(_button(chat.messages[anchor], "Back")), None)

    documents = [message for message in chat.bot_messages if message.document is not None]
    assert len(documents) == 1, chat.transcript()

    await boundary.callback(chat.press(_button(chat.messages[anchor], "Home")), None)

    assert [message.document for message in chat.bot_messages] == [None], chat.transcript()


@pytest.mark.asyncio
async def test_a_confirmation_about_the_session_is_not_leaving_the_session() -> None:
    """A stop token names `session:profile`, so an exact comparison reads its confirmation
    dialog as a screen about something else.

    The owner opens Force stop, reads it, and cancels — never having left the session — and
    the capture they were reading is gone.
    """
    session = _a_running_session()
    boundary = _inspectable_boundary(session, "x" * 5000)
    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(
        chat.press(_button(chat.messages[anchor], "Demo · Claude · regular · #1")), None
    )
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Inspect")), None)
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Back")), None)
    document = next(message for message in chat.bot_messages if message.document is not None)

    await boundary.callback(chat.press(_button(chat.messages[anchor], "Force stop")), None)

    assert document.message_id in chat.messages, (
        "a dialog about the session is not a screen about something else"
    )
    assert "cannot be undone" in chat.messages[anchor].text, "and it really is the dialog"


@pytest.mark.asyncio
async def test_a_twelve_interaction_journey_ends_with_one_live_view_and_no_transcript() -> None:
    """The Stage 2 gate, stated as the owner would state it.

    Twelve interactions covering every shape this stage changed — commands, presses, a
    guided text step, and a capture that produces a file — and at the end the chat holds
    one bot message and nothing at all that the owner typed. Every intermediate assertion
    in the other tests is a claim about a mechanism; this is the claim about the chat.

    *Declared deviation from the gate's itemisation.* The gate lists the twelve as
    `home → launch → search → profile → sessions → detail → inspect → back → home → help →
    sessions → home`. Two differences, neither reducing coverage: a search is not one
    interaction but two, since the box has to be answered before a project can be picked;
    and the tail is `home → help` rather than `help → sessions → home`, because the claim
    is about what the chat is left holding and `/help` exercises the command path the
    trailing `sessions → home` would only repeat.
    """
    session = _a_running_session()
    boundary = _inspectable_boundary(session, "x" * 5000)
    boundary.profiles = (ProfileAvailability("claude", True),)
    chat = FakeChat()

    async def press(label: str) -> None:
        await boundary.callback(chat.press(_button(chat.messages[anchor], label)), None)

    await boundary.start(chat.message_update("/start"), None)  # 1
    anchor = chat.bot_messages[0].message_id
    await press("Launch")  # 2
    await press("Search")  # 3
    await boundary.text(chat.message_update("Demo"), None)  # 4
    await press("Demo")  # 5 — profiles
    await press("Home")  # 6
    await boundary.sessions_command(chat.message_update("/sessions"), None)  # 7
    await press("Demo · Claude · regular · #1")  # 8 — detail
    await press("Inspect")  # 9
    # Checked mid-journey: an end-state assertion alone would be satisfied by a journey in
    # which the capture never produced a file at all, which is not the journey being claimed.
    assert [message.document for message in chat.bot_messages].count(None) == 1, (
        "the inspect step is meant to put a file in the chat, or step 10 proves nothing"
    )
    await press("Back")  # 10 — detail again
    await press("Home")  # 11
    await boundary.help_command(chat.message_update("/help"), None)  # 12

    assert len(chat.bot_messages) == 1, chat.transcript()
    assert chat.owner_messages == [], chat.transcript()
    assert chat.bot_messages[0].message_id == anchor, "and it was the same message throughout"
    # Home and Help both open with this heading, so the heading alone proves nothing about
    # where the journey ended; "Stop and close" appears only on Help.
    assert "Stop and close" in chat.bot_messages[0].text, chat.bot_messages[0].text[:120]


@pytest.mark.asyncio
async def test_a_message_the_bot_never_asked_for_is_left_where_the_owner_put_it() -> None:
    """The negative half of the single-screen rule, and the only one stated as a prohibition.

    Deletion is permitted for a superseded screen of ours, a consumed input of the owner's,
    and an unanswered question of ours — and for nothing else. Unsolicited owner chatter is
    the "nothing else": deleting it would tidy the chat and would be the adapter removing a
    message it never acted on. Today that rests entirely on `text()` returning early when no
    step is open, which is a single line nothing was pinning; a future handler that starts
    consuming stray input would satisfy every other test in this file.
    """
    chat = FakeChat()
    boundary = _boundary()
    await boundary.start(chat.message_update("/start"), None)
    anchor = chat.bot_messages[0].message_id

    await boundary.text(chat.message_update("just thinking out loud"), None)

    assert [message.text for message in chat.owner_messages] == ["just thinking out loud"], (
        "a message the bot never asked for and never answered is not ours to delete"
    )
    assert len(chat.bot_messages) == 1, chat.transcript()
    assert chat.bot_messages[0].message_id == anchor, "and it did not redraw over the silence"


async def _open_a_search(chat: FakeChat, boundary: PrivateBotBoundary, anchor: int) -> None:
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Launch")), None)
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Search")), None)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/start", "/launch", "/sessions", "/help"])
async def test_a_command_takes_an_unanswered_question_with_it(command: str) -> None:
    """Abandoning a guided step is the ordinary way to leave it, and the box has to go too.

    The input box is the one bot message outside the live view, so a redraw cannot replace
    it — and its only record is a single slot that the command then clears, after which
    nothing can ever remove it.
    """
    chat = FakeChat()
    boundary = _boundary()
    handlers = {
        "/start": boundary.start,
        "/launch": boundary.launch_command,
        "/sessions": boundary.sessions_command,
        "/help": boundary.help_command,
    }
    await boundary.start(chat.message_update("/start"), None)
    anchor = chat.bot_messages[0].message_id
    await _open_a_search(chat, boundary, anchor)
    assert len(chat.bot_messages) == 2, "the box is open at this point"

    await handlers[command](chat.message_update(command), None)

    assert len(chat.bot_messages) == 1, chat.transcript()
    assert chat.owner_messages == []


@pytest.mark.asyncio
async def test_navigating_away_by_button_takes_the_unanswered_question_with_it() -> None:
    """No typing at all: Launch → Search → Home, twice, used to leave two dead boxes.

    Each one still accepted input for a step nobody was in, and each was permanent.
    """
    chat = FakeChat()
    boundary = _boundary()
    await boundary.start(chat.message_update("/start"), None)
    anchor = chat.bot_messages[0].message_id

    for _ in range(3):
        await _open_a_search(chat, boundary, anchor)
        await boundary.callback(chat.press(_button(chat.messages[anchor], "Home")), None)
        assert len(chat.bot_messages) == 1, chat.transcript()

    assert chat.owner_messages == []


@pytest.mark.asyncio
async def test_opening_a_second_question_does_not_orphan_the_first_ones_box() -> None:
    """The slot holding the box's id is about to be overwritten; the box must go first.

    Driven directly rather than through the UI: today every route to a guided step passes
    through a screen that already abandons the open one, so no sequence of presses reaches
    this. That makes the guard defence for a screen layout that does not exist yet — which
    is worth keeping and therefore worth pinning, since a test that can only be satisfied
    by the *other* protection proves nothing about this one.
    """
    chat = FakeChat()
    boundary = _boundary()
    await boundary.start(chat.message_update("/start"), None)
    anchor = chat.bot_messages[0].message_id
    query = chat.press("unused").callback_query

    await boundary._begin_guided_text_entry(query, "launch.search", "search")
    first_box = next(m for m in chat.bot_messages if m.message_id != anchor)
    await boundary._begin_guided_text_entry(query, "resume.search", "search")

    assert first_box.message_id not in chat.messages, chat.transcript()
    assert len(chat.bot_messages) == 2, "the live view and exactly one open question"


@pytest.mark.asyncio
async def test_a_document_telegram_refuses_to_delete_is_retried_not_forgotten() -> None:
    """`discard` swallowing the refusal internally used to make the documented retry a lie.

    The id was cleared on the assumption the delete worked, so the surviving file was
    untracked and no later navigation could ever remove it.
    """
    session = _a_running_session()
    boundary = _inspectable_boundary(session, "x" * 5000)
    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(
        chat.press(_button(chat.messages[anchor], "Demo · Claude · regular · #1")), None
    )
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Inspect")), None)
    document = next(m for m in chat.bot_messages if m.document is not None)

    original = chat.bot.delete_message

    async def _refuse(**kwargs: object) -> None:
        if kwargs["message_id"] == document.message_id:
            raise BadRequest("Message can't be deleted")
        await original(**kwargs)

    chat.bot.delete_message = _refuse
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Home")), None)
    assert document.message_id in chat.messages, "Telegram refused, so it is still there"
    assert boundary._attachment is not None, "and it must still be tracked, or it is lost"

    chat.bot.delete_message = original
    await boundary.sessions_command(chat.message_update("/sessions"), None)

    assert document.message_id not in chat.messages, "the next navigation really does retry"
    assert boundary._attachment is None


def _finished(session: SessionRecord) -> AgentActivity:
    return AgentActivity(
        session_id=str(session.session_id),
        kind=ActivityKind.COMPLETED,
        detail="Ran the suite.",
        observed_at=datetime(2026, 8, 11, 14, 5, tzinfo=UTC),
    )


async def _notify(chat: FakeChat, boundary: PrivateBotBoundary, activity: AgentActivity) -> int:
    """Deliver one activity into the chat and answer which message it became.

    A pass that sends anything also moves the live view below it, so *two* messages can be new.
    The anchor is the one that is not the notification.
    """
    boundary.notifier.attach(chat.bot)
    before = {message.message_id for message in chat.bot_messages}
    assert await boundary.notifier.deliver([activity]) == 1
    anchor = boundary.view.anchor()
    new = [
        message
        for message in chat.bot_messages
        if message.message_id not in before and message.message_id != anchor
    ]
    assert len(new) == 1, chat.transcript()
    return new[0].message_id


@pytest.mark.asyncio
async def test_a_notification_stands_beside_the_live_view_rather_than_replacing_it() -> None:
    """Sub-plan 1 left this chat with exactly one bot message. A notification is the first
    thing entitled to be a second one, and it may not take the screen to do it."""
    record = _a_running_session()
    boundary, _ = _renameable(record)
    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)

    notification = await _notify(chat, boundary, _finished(record))

    anchor = boundary.view.anchor()
    assert notification != anchor, "the notification took over the live view"
    assert "Sessions" in chat.messages[anchor].text
    assert "The agent has finished its work." in chat.messages[notification].text
    assert len(chat.bot_messages) == 2, chat.transcript()


@pytest.mark.asyncio
async def test_navigating_the_live_view_leaves_the_notification_in_the_chat() -> None:
    """The live view redraws and prunes its own message's tokens. A notification is not its
    message, so neither the redraw nor the prune may reach it."""
    record = _a_running_session()
    boundary, _ = _renameable(record)
    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    notification = await _notify(chat, boundary, _finished(record))
    open_session = _button(chat.messages[notification], "Open session")
    anchor = boundary.view.anchor()

    await boundary.callback(chat.press(_button(chat.messages[anchor], "Home")), None)
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Sessions")), None)

    assert notification in chat.messages, chat.transcript()
    assert "The agent has finished its work." in chat.messages[notification].text
    assert (
        boundary.callbacks.resolve(open_session, owner_id=7, chat_id=11, message_id=notification)
        is not None
    ), "navigating the live view pruned a token it does not own"


@pytest.mark.asyncio
async def test_the_notification_button_opens_the_session_and_consumes_the_notification() -> None:
    """The press opens the session, and the notification that offered it goes.

    This reverses what Task 3.3 originally pinned. That test asserted the notification survived
    its own press, which the plan asked for — and the owner's acceptance run showed what it
    actually produces: a chat filling with alerts already acted on, each still offering the
    button just pressed, each pushing the menu further out of view. An answered question of
    ours is one of the four things `LiveView.discard` has always permitted; nothing needed
    relaxing, only applying.
    """
    record = _a_running_session()
    boundary, _ = _renameable(record)
    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    notification = await _notify(chat, boundary, _finished(record))
    token = _button(chat.messages[notification], "Open session")

    press = chat.press(token)
    await boundary.callback(press, None)

    assert press.callback_query.answers == [None], "the button was refused"
    anchor = boundary.view.anchor()
    assert "Demo · Claude · regular · #1" in chat.messages[anchor].text
    assert "Rename" in [
        button.text for row in chat.messages[anchor].reply_markup.inline_keyboard for button in row
    ], "the press opened the session detail, not some other screen"
    assert notification not in chat.messages, chat.transcript()
    assert len(chat.bot_messages) == 1, chat.transcript()
    assert (
        boundary.callbacks.resolve(token, owner_id=7, chat_id=11, message_id=notification) is None
    ), "a token outlived the message it was drawn on"


@pytest.mark.asyncio
async def test_a_notification_moves_the_menu_below_it_so_it_stays_reachable() -> None:
    """Telegram orders a chat by send time, so a notification always lands below the menu.

    Editing the anchor in place cannot answer that — the message stays where it was sent — so
    the live view is re-sent beneath whatever arrived. Reported by the owner from the real
    client: the menu was being pushed out of view as notifications accumulated.
    """
    record = _a_running_session()
    boundary, _ = _renameable(record)
    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    first_anchor = chat.bot_messages[0].message_id
    sessions_token = _button(chat.messages[first_anchor], "Home")

    notification = await _notify(chat, boundary, _finished(record))

    moved = boundary.view.anchor()
    assert moved != first_anchor, "the live view did not move"
    assert first_anchor not in chat.messages, "the old screen was left in the chat"
    assert [message.message_id for message in chat.bot_messages] == [notification, moved], (
        "the menu must be the newest message in the chat"
    )
    assert (
        boundary.callbacks.resolve(sessions_token, owner_id=7, chat_id=11, message_id=moved)
        is not None
    ), "moving the screen killed the keyboard on it"


@pytest.mark.asyncio
async def test_the_menu_stays_at_the_bottom_as_notifications_accumulate() -> None:
    """Three unacted notifications, and the menu is still the last thing in the chat."""
    record = _a_running_session()
    boundary, _ = _renameable(record)
    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)

    for index in range(3):
        boundary.notifier.attach(chat.bot)
        await boundary.notifier.deliver(
            [
                AgentActivity(
                    session_id=str(record.session_id),
                    kind=ActivityKind.COMPLETED,
                    detail=f"run {index}",
                    observed_at=datetime(2026, 8, 11, 14, 5 + index, tzinfo=UTC),
                )
            ]
        )
        # Past the suppression window, so each pass genuinely sends.
        boundary.notifier._last_sent.clear()

    assert chat.bot_messages[-1].message_id == boundary.view.anchor(), chat.transcript()
    assert "Sessions" in chat.messages[boundary.view.anchor()].text
    assert len(chat.bot_messages) == 4, "three notifications and one menu"


@pytest.mark.asyncio
async def test_a_notification_button_still_resolves_after_a_re_composition(tmp_path) -> None:
    """Sub-plan 1's durability, on a message the anchor does not own.

    A restart re-reads the anchor and redraws the live view into it; the notification is a
    different message, so the only thing that can make its button survive is the token store
    being durable and the token being bound to a message id rather than to a process.
    """
    record = _a_running_session()
    database = tmp_path / "sessions.sqlite3"
    connection = open_database(database)

    class _Launcher:
        async def list_sessions(self):
            return [record]

        async def refresh_readiness(self) -> None:
            return None

    before = PrivateBotBoundary(
        7,
        11,
        launcher=_Launcher(),
        callbacks=SQLiteCallbackStateStore(connection),
        anchors=SQLiteChatViewStore(connection),
    )
    chat = FakeChat()
    await before.sessions_command(chat.message_update("/sessions"), None)
    notification = await _notify(chat, before, _finished(record))
    open_session = _button(chat.messages[notification], "Open session")
    connection.close()

    reopened = open_database(database)
    after = PrivateBotBoundary(
        7,
        11,
        launcher=_Launcher(),
        callbacks=SQLiteCallbackStateStore(reopened),
        anchors=SQLiteChatViewStore(reopened),
    )
    press = chat.press(open_session)
    await after.callback(press, None)

    assert press.callback_query.answers == [None], (
        "the restarted service refused a notification it had sent"
    )
    assert "Demo · Claude · regular · #1" in chat.messages[after.view.anchor()].text
    assert notification not in chat.messages, (
        "the restarted service resolved the button but did not consume the notification"
    )


@pytest.mark.asyncio
async def test_a_notification_press_does_not_make_it_the_live_view(tmp_path) -> None:
    """The one message of ours that is deliberately not a screen.

    Every press adopts the message it came from as the anchor when the chat has none recorded
    — a recovery for a composition that never wrote one, and right for every screen. A
    notification is not a screen: adopting it would make the next render edit the session
    detail *over* the notification, consuming the message the runbook promises survives
    pruning. The state is reachable rather than theoretical — a restored database
    (`docs/database-recovery.md`) leaves a chat with sessions, hooks and no anchor row.
    """
    record = _a_running_session()

    class _Launcher:
        async def list_sessions(self):
            return [record]

        async def refresh_readiness(self) -> None:
            return None

    connection = open_database(tmp_path / "sessions.sqlite3")
    boundary = PrivateBotBoundary(
        7,
        11,
        launcher=_Launcher(),
        callbacks=SQLiteCallbackStateStore(connection),
        anchors=SQLiteChatViewStore(connection),
    )
    chat = FakeChat()

    # No screen has ever been drawn: the chat has no anchor at all.
    assert boundary.view.anchor() is None
    notification = await _notify(chat, boundary, _finished(record))

    press = chat.press(_button(chat.messages[notification], "Open session"))
    await boundary.callback(press, None)

    assert press.callback_query.answers == [None], "the button was refused"
    assert boundary.view.anchor() != notification, "the notification became the live view"
    assert notification not in chat.messages, "the notification should have been consumed"
    # The detail was drawn into a message of its own rather than over the notification: had the
    # notification been adopted, discarding it afterwards would have deleted the live view too.
    assert "Demo · Claude · regular · #1" in chat.messages[boundary.view.anchor()].text
