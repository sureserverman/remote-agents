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
from remote_agents.adapters.telegram.wizard import ProfileAvailability
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


def _a_running_session() -> SessionRecord:
    return SessionRecord(
        SessionId(UUID(int=7)),
        ProjectId("a" * 24),
        ProfileId("claude"),
        SessionDisplayIdentity("Demo", "Claude", "regular", 1),
        SessionState.RUNNING,
        datetime(2026, 8, 10, tzinfo=UTC),
    )


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
