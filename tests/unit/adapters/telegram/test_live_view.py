"""The chat holds one message, and every screen is that message being redrawn."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.live_view import ChatViewStore, LiveView

CHAT = 11
OWNER = 7


class _Bot:
    """The Telegram surface a live view actually uses, and nothing else.

    Deliberately not a `Message` double: the live view addresses a message id in a chat
    rather than replying to whatever arrived, which is the whole difference between one
    screen being redrawn and a transcript accumulating.
    """

    def __init__(
        self,
        *,
        first_id: int = 100,
        edit_error: Exception | None = None,
        delete_error: Exception | None = None,
    ) -> None:
        self.sent: list[dict[str, object]] = []
        self.edits: list[dict[str, object]] = []
        self.deleted: list[dict[str, object]] = []
        self.edit_error = edit_error
        self.delete_error = delete_error
        self._next_id = first_id

    async def send_message(self, **kwargs: object) -> SimpleNamespace:
        self.sent.append(kwargs)
        message_id = self._next_id
        self._next_id += 1
        return SimpleNamespace(message_id=message_id)

    async def edit_message_text(self, **kwargs: object) -> None:
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(kwargs)

    async def delete_message(self, **kwargs: object) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(kwargs)


def _view(
    *, callbacks: CallbackStateStore | None = None, anchors: ChatViewStore | None = None
) -> LiveView:
    return LiveView(
        chat_id=CHAT,
        callbacks=callbacks or CallbackStateStore(),
        anchors=anchors or ChatViewStore(),
    )


@pytest.mark.asyncio
async def test_a_first_render_sends_the_message_and_records_it_as_the_anchor() -> None:
    anchors = ChatViewStore()
    bot = _Bot(first_id=100)
    view = _view(anchors=anchors)

    message_id = await view.render(bot, {"text": "Home"})

    assert message_id == 100
    assert anchors.anchor(CHAT) == 100
    assert [sent["text"] for sent in bot.sent] == ["Home"]
    assert bot.sent[0]["chat_id"] == CHAT
    assert bot.edits == []


@pytest.mark.asyncio
async def test_a_second_render_edits_the_recorded_anchor_rather_than_sending_again() -> None:
    anchors = ChatViewStore()
    bot = _Bot(first_id=100)
    view = _view(anchors=anchors)
    await view.render(bot, {"text": "Home"})

    message_id = await view.render(bot, {"text": "Sessions"})

    assert message_id == 100, "the anchor is redrawn, never replaced"
    assert len(bot.sent) == 1, "a second screen must not become a second message"
    assert bot.edits == [{"chat_id": CHAT, "message_id": 100, "text": "Sessions"}]
    assert anchors.anchor(CHAT) == 100


@pytest.mark.asyncio
async def test_a_render_that_changes_nothing_is_guarded_and_raises_nothing() -> None:
    """The dead-button gotcha: a no-op edit *raises* rather than doing nothing.

    Telegram answers an identical edit with `Message is not modified`, and letting that
    reach the handler is what strands the owner on a screen whose buttons appear dead.
    """
    anchors = ChatViewStore()
    anchors.record_anchor(CHAT, 100)
    bot = _Bot(edit_error=BadRequest("Message is not modified: specified new message content"))
    view = _view(anchors=anchors)

    message_id = await view.render(bot, {"text": "Home"})

    assert message_id == 100
    assert anchors.anchor(CHAT) == 100, "a refused no-op leaves the anchor exactly where it was"


@pytest.mark.asyncio
async def test_a_refused_no_op_keeps_the_buttons_that_are_still_on_screen() -> None:
    """Nothing changed on screen, so nothing on screen may stop resolving."""
    anchors = ChatViewStore()
    anchors.record_anchor(CHAT, 100)
    callbacks = CallbackStateStore()
    standing = callbacks.create("nav.home", "home", OWNER, CHAT)
    callbacks.bind_pending(CHAT, 100)
    bot = _Bot(edit_error=BadRequest("Message is not modified"))
    view = _view(callbacks=callbacks, anchors=anchors)

    await view.render(bot, {"text": "Home"})

    assert callbacks.resolve(standing, owner_id=OWNER, chat_id=CHAT, message_id=100) is not None, (
        "pruning a screen the edit never replaced is what kills a live button"
    )


@pytest.mark.asyncio
async def test_a_render_retires_the_replaced_keyboard_and_adopts_the_drawn_one() -> None:
    """Edit, then prune, then bind — the order is the whole correctness argument."""
    anchors = ChatViewStore()
    callbacks = CallbackStateStore()
    bot = _Bot(first_id=100)
    view = _view(callbacks=callbacks, anchors=anchors)
    first = callbacks.create("nav.home", "home", OWNER, CHAT)
    await view.render(bot, {"text": "Home"})
    second = callbacks.create("sessions.open", "sessions", OWNER, CHAT)

    await view.render(bot, {"text": "Sessions"})

    assert callbacks.resolve(first, owner_id=OWNER, chat_id=CHAT, message_id=100) is None
    assert callbacks.resolve(second, owner_id=OWNER, chat_id=CHAT, message_id=100) is not None


@pytest.mark.asyncio
async def test_an_edit_failure_that_is_not_a_no_op_is_not_swallowed() -> None:
    """Only the identical-content refusal is benign; Task 2.2 answers the rest."""
    anchors = ChatViewStore()
    anchors.record_anchor(CHAT, 100)
    bot = _Bot(edit_error=BadRequest("CHAT_WRITE_FORBIDDEN"))
    view = _view(anchors=anchors)

    with pytest.raises(BadRequest):
        await view.render(bot, {"text": "Home"})


@pytest.mark.asyncio
async def test_an_interstitial_render_leaves_the_action_it_interrupts_still_claimable() -> None:
    """The wait screen must not prune the token whose action it is waiting for.

    The pressed token is bound to this message, so a retiring render would discard it
    before the action it names has been claimed — the button resolves, the wait appears,
    and nothing ever happens.
    """
    anchors = ChatViewStore()
    anchors.record_anchor(CHAT, 100)
    callbacks = CallbackStateStore()
    in_flight = callbacks.create("session.graceful", "abc", OWNER, CHAT, mutation=True)
    callbacks.bind_pending(CHAT, 100)
    view = _view(callbacks=callbacks, anchors=anchors)

    await view.render(_Bot(), {"text": "Stopping the session…"}, retire=False)

    assert callbacks.claim_mutation(in_flight, owner_id=OWNER, chat_id=CHAT, message_id=100)


@pytest.mark.asyncio
async def test_a_retiring_render_is_what_discards_the_keyboard_it_replaces() -> None:
    """The opposite of the case above, so `retire` is proved to mean something."""
    anchors = ChatViewStore()
    anchors.record_anchor(CHAT, 100)
    callbacks = CallbackStateStore()
    replaced = callbacks.create("session.graceful", "abc", OWNER, CHAT, mutation=True)
    callbacks.bind_pending(CHAT, 100)
    view = _view(callbacks=callbacks, anchors=anchors)

    await view.render(_Bot(), {"text": "Sessions"})

    assert not callbacks.claim_mutation(replaced, owner_id=OWNER, chat_id=CHAT, message_id=100)


def test_adopting_recovers_an_anchor_that_was_never_recorded() -> None:
    anchors = ChatViewStore()
    view = _view(anchors=anchors)

    view.adopt(100)

    assert anchors.anchor(CHAT) == 100


def test_adopting_never_walks_a_recorded_anchor_onto_an_older_screen() -> None:
    anchors = ChatViewStore()
    anchors.record_anchor(CHAT, 100)
    view = _view(anchors=anchors)

    view.adopt(50)

    assert anchors.anchor(CHAT) == 100


def test_the_anchor_store_answers_nothing_for_a_chat_it_has_never_drawn_into() -> None:
    assert ChatViewStore().anchor(CHAT) is None


@pytest.mark.parametrize("message_id", [0, -1])
@pytest.mark.parametrize("record", ["record_anchor", "adopt_anchor"])
def test_the_in_memory_store_refuses_the_unbound_sentinel_as_its_sibling_does(
    record: str, message_id: int
) -> None:
    """Zero is `UNBOUND`. Anchoring to it would address a message that does not exist."""
    with pytest.raises(ValueError):
        getattr(ChatViewStore(), record)(CHAT, message_id)


def test_adopting_an_anchor_reports_whether_it_was_the_one_that_took() -> None:
    """One statement, one answer — the caller never re-reads to find out who won."""
    anchors = ChatViewStore()

    assert anchors.adopt_anchor(CHAT, 100) is True
    assert anchors.adopt_anchor(CHAT, 205) is False
    assert anchors.anchor(CHAT) == 100


def test_adopting_answers_false_even_for_the_anchor_already_stored() -> None:
    """The same narrowed contract its SQLite sibling gives — True means *this* call wrote.

    Pinned on both implementations because a port whose two halves answer differently is a
    contract only one caller at a time can rely on.
    """
    anchors = ChatViewStore()
    anchors.adopt_anchor(CHAT, 100)

    assert anchors.adopt_anchor(CHAT, 100) is False
    assert anchors.anchor(CHAT) == 100


# --- Task 2.2: the 48-hour edit window, and a message that is simply gone ------------------

REFUSALS = [
    "Message can't be edited",
    "Message to edit not found",
    "MESSAGE_ID_INVALID",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", REFUSALS)
async def test_a_resend_answers_an_edit_telegram_refuses(refusal: str) -> None:
    """Past 48 hours Telegram will not edit a message. The button must not notice.

    The owner asked for a screen; they get a screen. That the old message could not be
    edited is an implementation detail of Telegram's retention, not news for the owner.
    """
    anchors = ChatViewStore()
    anchors.record_anchor(CHAT, 100)
    bot = _Bot(first_id=500, edit_error=BadRequest(refusal))
    view = _view(anchors=anchors)

    message_id = await view.render(bot, {"text": "Sessions"})

    assert message_id == 500, "the live view moved to the message that could be written"
    assert [sent["text"] for sent in bot.sent] == ["Sessions"]
    assert anchors.anchor(CHAT) == 500, "chat_views follows the screen, or the next edit is lost"
    assert bot.deleted == [{"chat_id": CHAT, "message_id": 100}], (
        "leaving the retired message would leave a second screen in the chat"
    )


@pytest.mark.asyncio
async def test_a_resend_retires_the_dead_message_s_tokens_and_binds_the_new_screen_s() -> None:
    anchors = ChatViewStore()
    anchors.record_anchor(CHAT, 100)
    callbacks = CallbackStateStore()
    retired = callbacks.create("nav.home", "home", OWNER, CHAT)
    callbacks.bind_pending(CHAT, 100)
    drawn = callbacks.create("sessions.open", "sessions", OWNER, CHAT)
    bot = _Bot(first_id=500, edit_error=BadRequest("Message can't be edited"))
    view = _view(callbacks=callbacks, anchors=anchors)

    await view.render(bot, {"text": "Sessions"})

    assert callbacks.resolve(retired, owner_id=OWNER, chat_id=CHAT, message_id=100) is None
    assert callbacks.resolve(drawn, owner_id=OWNER, chat_id=CHAT, message_id=500) is not None


@pytest.mark.asyncio
async def test_a_resend_survives_a_message_that_someone_already_deleted() -> None:
    """The refusal and the failed delete are the same fact arriving twice."""
    anchors = ChatViewStore()
    anchors.record_anchor(CHAT, 100)
    bot = _Bot(
        first_id=500,
        edit_error=BadRequest("Message to edit not found"),
        delete_error=BadRequest("Message to delete not found"),
    )
    view = _view(anchors=anchors)

    assert await view.render(bot, {"text": "Sessions"}) == 500
    assert anchors.anchor(CHAT) == 500


@pytest.mark.asyncio
async def test_an_interstitial_resend_keeps_the_action_it_is_waiting_for_claimable() -> None:
    """A button on a three-day-old screen is exactly what Stage 1 made possible.

    Pressing it refuses the interstitial edit and forces a re-send *while the action is in
    flight* — so this is the one resend that must not retire the pressed token, which is
    still bound to the message being deleted.
    """
    anchors = ChatViewStore()
    anchors.record_anchor(CHAT, 100)
    callbacks = CallbackStateStore()
    in_flight = callbacks.create("session.graceful", "abc", OWNER, CHAT, mutation=True)
    callbacks.bind_pending(CHAT, 100)
    bot = _Bot(first_id=500, edit_error=BadRequest("Message can't be edited"))
    view = _view(callbacks=callbacks, anchors=anchors)

    await view.render(bot, {"text": "Stopping the session…"}, retire=False)

    assert callbacks.claim_mutation(in_flight, owner_id=OWNER, chat_id=CHAT, message_id=100), (
        "the stop resolved and showed its wait screen; pruning here is what makes it vanish"
    )


@pytest.mark.asyncio
async def test_the_next_retiring_render_collects_the_tokens_an_interstitial_resend_left() -> None:
    """The deferred half of the case above: kept alive for the action, not forever."""
    anchors = ChatViewStore()
    anchors.record_anchor(CHAT, 100)
    callbacks = CallbackStateStore()
    stranded = callbacks.create("session.graceful", "abc", OWNER, CHAT, mutation=True)
    callbacks.bind_pending(CHAT, 100)
    view = _view(callbacks=callbacks, anchors=anchors)
    await view.render(
        _Bot(first_id=500, edit_error=BadRequest("Message can't be edited")),
        {"text": "Stopping the session…"},
        retire=False,
    )

    await view.render(_Bot(), {"text": "The session has ended."})

    assert callbacks.resolve(stranded, owner_id=OWNER, chat_id=CHAT, message_id=100) is None
    assert callbacks.active_count() == 0


@pytest.mark.asyncio
async def test_a_retiring_render_that_is_itself_a_resend_still_collects_what_is_owed() -> None:
    """The collecting render can refuse too, and it is the one that must not skip the drain.

    Reachable: the owner deletes the freshly re-sent interstitial in the seconds before the
    action returns, so the render carrying the result is refused in turn. Draining only
    inside the ordinary edit path skips the deferred prune exactly when two refusals land
    back to back.
    """
    anchors = ChatViewStore()
    anchors.record_anchor(CHAT, 100)
    callbacks = CallbackStateStore()
    stranded = callbacks.create("session.graceful", "abc", OWNER, CHAT, mutation=True)
    callbacks.bind_pending(CHAT, 100)
    view = _view(callbacks=callbacks, anchors=anchors)
    await view.render(
        _Bot(first_id=500, edit_error=BadRequest("Message can't be edited")),
        {"text": "Stopping the session…"},
        retire=False,
    )

    await view.render(
        _Bot(first_id=900, edit_error=BadRequest("Message to edit not found")),
        {"text": "The session has ended."},
    )

    assert callbacks.resolve(stranded, owner_id=OWNER, chat_id=CHAT, message_id=100) is None
    assert callbacks.active_count() == 0


@pytest.mark.asyncio
async def test_two_interstitial_resends_in_a_row_lose_neither_deferred_prune() -> None:
    """One owed prune overwriting another is how the first message's tokens outlive it."""
    anchors = ChatViewStore()
    anchors.record_anchor(CHAT, 100)
    callbacks = CallbackStateStore()
    first = callbacks.create("session.graceful", "abc", OWNER, CHAT, mutation=True)
    callbacks.bind_pending(CHAT, 100)
    view = _view(callbacks=callbacks, anchors=anchors)
    await view.render(
        _Bot(first_id=500, edit_error=BadRequest("Message can't be edited")),
        {"text": "Stopping…"},
        retire=False,
    )
    second = callbacks.create("session.cleanup", "abc", OWNER, CHAT, mutation=True)
    callbacks.bind_pending(CHAT, 500)
    await view.render(
        _Bot(first_id=700, edit_error=BadRequest("Message can't be edited")),
        {"text": "Cleaning up…"},
        retire=False,
    )

    await view.render(_Bot(), {"text": "Done."})

    assert callbacks.resolve(first, owner_id=OWNER, chat_id=CHAT, message_id=100) is None
    assert callbacks.resolve(second, owner_id=OWNER, chat_id=CHAT, message_id=500) is None
    assert callbacks.active_count() == 0
