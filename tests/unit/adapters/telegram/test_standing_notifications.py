"""One notification per session — across a restart, and gone once the session has finished.

Both cases here were reported from the chat rather than found by reading the code, and both are
about the same piece of state: which message a session's notification *is*.

The first is the 2026-08-20 transcript. The service restarted at 21:23; the session it had
already sent a notification about reported again at 21:35; the notifier, having held that
message id in process memory, sent a second message. The live view could not even collect the
first — `LiveView._last_arguments` is process-local too, so `move_to_bottom` declined after the
restart — leaving the owner one notification above the menu and one below it, for one session.

The second is the same state read the other way round: a session the owner has stopped has
already answered the question its notification asked, so the message is obsolete and goes. The
observation stays where the local feed reads it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.notifications import (
    ActivityNotifier,
    StandingNotificationStore,
)
from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind, AgentActivity

SESSION = "7a729881-8115-41fb-8613-160182188f40"
DISPLAY = "remote-agents · claude · main · #4"
STARTED = datetime(2026, 8, 20, 21, 13, tzinfo=UTC)


def _activity(
    kind: ActivityKind = ActivityKind.COMPLETED,
    *,
    detail: str | None = None,
    minutes: int = 0,
    session_id: str = SESSION,
) -> AgentActivity:
    return AgentActivity(
        session_id=session_id,
        kind=kind,
        detail=detail,
        observed_at=STARTED + timedelta(minutes=minutes),
        confidence=ActivityConfidence.REPORTED,
    )


class _Chat:
    """The Telegram chat, which outlives any one process that speaks into it.

    Shared by both notifiers in the restart cases on purpose: the point of those is that the
    *chat* is the thing with memory, and a restart must not make the service forget what is
    already in it.
    """

    chat_id = 11

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.amended: list[tuple[int, dict[str, object]]] = []
        self.deleted: list[int] = []
        self.moved = 0
        self._next_id = 1100

    async def send_apart(self, _bot: object, arguments: dict[str, object]) -> int:
        self._next_id += 1
        self.sent.append(arguments)
        return self._next_id

    async def amend_apart(
        self, _bot: object, message_id: int, arguments: dict[str, object]
    ) -> bool:
        self.amended.append((message_id, arguments))
        return True

    async def discard(self, _bot: object, message_id: int) -> bool:
        self.deleted.append(message_id)
        return True

    async def move_to_bottom(self, _bot: object) -> int | None:
        self.moved += 1
        return None

    def standing_messages(self) -> int:
        """How many of this session's notifications are in the chat, not how many were sent."""
        return len(self.sent) - len(self.deleted)


class _SilentBot:
    async def edit_message_reply_markup(self, **_kwargs: object) -> None:
        return None


def _notifier(
    chat: _Chat,
    standing: StandingNotificationStore,
    *,
    callbacks: CallbackStateStore,
    finished: tuple[str, ...] = (),
    now: datetime = STARTED,
):
    """One process's notifier, speaking into `chat` and remembering through `standing`."""

    async def display(_session_id: str) -> str:
        return DISPLAY

    async def which_finished(session_values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value for value in session_values if value in finished)

    notifier = ActivityNotifier(
        view=chat,
        callbacks=callbacks,
        owner_user_id=7,
        display=display,
        standing=standing,
        finished=which_finished,
        now=lambda: now,
    )
    notifier.attach(_SilentBot())
    return notifier


@pytest.mark.asyncio
async def test_a_restart_amends_the_notification_it_already_sent(tmp_path) -> None:
    """The reported defect. Two reports from one session, a restart between them, and the
    owner is owed exactly one message — not one above the menu and one below."""
    chat, standing, callbacks = _Chat(), StandingNotificationStore(), CallbackStateStore()
    before = _notifier(chat, standing, callbacks=callbacks)

    assert await before.deliver([_activity(detail="Found it.")]) == 1
    assert len(chat.sent) == 1

    # The restart: a new process, the same chat and the same durable record of it.
    after = _notifier(chat, standing, callbacks=callbacks, now=STARTED + timedelta(minutes=23))
    assert await after.deliver([_activity(detail="It's working now.", minutes=23)]) == 1

    assert len(chat.sent) == 1, "the restart sent the session a second notification"
    assert chat.amended, "the surviving message was never brought up to date"
    assert chat.standing_messages() == 1


@pytest.mark.asyncio
async def test_a_restart_replaces_rather_than_accumulates_when_the_news_is_new() -> None:
    """A kind the message does not carry still earns a message that *arrives* — it is news the
    owner has not been alerted to. What it must not do is arrive beside the old one."""
    chat, standing, callbacks = _Chat(), StandingNotificationStore(), CallbackStateStore()
    before = _notifier(chat, standing, callbacks=callbacks)
    await before.deliver([_activity()])
    first = chat.sent[0]

    after = _notifier(chat, standing, callbacks=callbacks, now=STARTED + timedelta(minutes=23))
    await after.deliver([_activity(ActivityKind.NEEDS_ANSWER, minutes=23)])

    assert len(chat.sent) == 2, "the new kind was not put in front of the owner"
    assert chat.deleted, "the superseded notification was left in the chat"
    assert chat.standing_messages() == 1
    assert chat.sent[0] is first


@pytest.mark.asyncio
async def test_a_finished_session_loses_its_notification() -> None:
    """The owner stopped it, so the alert reports their own action back at them and goes."""
    chat, standing, callbacks = _Chat(), StandingNotificationStore(), CallbackStateStore()
    notifier = _notifier(chat, standing, callbacks=callbacks)
    await notifier.deliver([_activity()])
    message_id = standing.notification(chat.chat_id, SESSION).message_id

    stopped = _notifier(
        chat, standing, callbacks=callbacks, finished=(SESSION,), now=STARTED + timedelta(hours=1)
    )
    assert await stopped.retire_finished() == 1

    assert chat.deleted == [message_id]
    assert chat.standing_messages() == 0
    assert standing.notification(chat.chat_id, SESSION) is None
    assert callbacks.active_count() == 0, "a token outlived the message it was drawn on"


@pytest.mark.asyncio
async def test_a_running_session_keeps_its_notification() -> None:
    """The sweep is not a expiry: an agent still running is still worth opening."""
    chat, standing, callbacks = _Chat(), StandingNotificationStore(), CallbackStateStore()
    notifier = _notifier(chat, standing, callbacks=callbacks)
    await notifier.deliver([_activity()])

    assert await notifier.retire_finished() == 0
    assert chat.deleted == []
    assert standing.notification(chat.chat_id, SESSION) is not None


@pytest.mark.asyncio
async def test_a_delivery_pass_collects_a_finished_session_on_its_own() -> None:
    """The console stops sessions in another process, so nothing tells this one directly.
    A pass with nothing to deliver still has to collect what the other surface ended."""
    chat, standing, callbacks = _Chat(), StandingNotificationStore(), CallbackStateStore()
    await _notifier(chat, standing, callbacks=callbacks).deliver([_activity()])

    stopped = _notifier(chat, standing, callbacks=callbacks, finished=(SESSION,))
    assert await stopped.deliver([]) == 0

    assert chat.standing_messages() == 0


@pytest.mark.asyncio
async def test_one_session_finishing_leaves_another_session_alone() -> None:
    other = "51b582fd-68b5-4c52-afcd-9d5bf77bd2b6"
    chat, standing, callbacks = _Chat(), StandingNotificationStore(), CallbackStateStore()
    notifier = _notifier(chat, standing, callbacks=callbacks)
    await notifier.deliver([_activity(), _activity(session_id=other)])
    assert len(chat.sent) == 2

    stopped = _notifier(chat, standing, callbacks=callbacks, finished=(SESSION,))
    assert await stopped.retire_finished() == 1

    assert standing.notification(chat.chat_id, SESSION) is None
    assert standing.notification(chat.chat_id, other) is not None
    assert chat.standing_messages() == 1


@pytest.mark.asyncio
async def test_a_refused_delete_leaves_the_notification_to_be_collected_again() -> None:
    """The record is the only thing that says the message is ours to remove, so a refusal
    must not drop it — the next pass is the retry."""
    chat, standing, callbacks = _Chat(), StandingNotificationStore(), CallbackStateStore()
    notifier = _notifier(chat, standing, callbacks=callbacks)
    await notifier.deliver([_activity()])

    async def refuse(_bot: object, _message_id: int) -> bool:
        raise RuntimeError("Telegram refused the delete")

    stopped = _notifier(chat, standing, callbacks=callbacks, finished=(SESSION,))
    chat.discard = refuse  # type: ignore[method-assign]
    assert await stopped.retire_finished() == 0
    assert standing.notification(chat.chat_id, SESSION) is not None

    chat.discard = _Chat.discard.__get__(chat)  # type: ignore[method-assign]
    assert await stopped.retire_finished() == 1
    assert standing.notification(chat.chat_id, SESSION) is None


@pytest.mark.asyncio
async def test_a_lifecycle_that_will_not_answer_keeps_every_notification() -> None:
    """Deleting the owner's alerts on the strength of a failed read is not a trade worth
    making. A sweep that cannot ask leaves the chat exactly as it found it."""
    chat, standing, callbacks = _Chat(), StandingNotificationStore(), CallbackStateStore()
    await _notifier(chat, standing, callbacks=callbacks).deliver([_activity()])

    async def display(_session_id: str) -> str:
        return DISPLAY

    async def refuses(_session_values: tuple[str, ...]) -> tuple[str, ...]:
        raise RuntimeError("the session store did not answer")

    blind = ActivityNotifier(
        view=chat,
        callbacks=callbacks,
        owner_user_id=7,
        display=display,
        standing=standing,
        finished=refuses,
        now=lambda: STARTED,
    )
    blind.attach(_SilentBot())

    assert await blind.retire_finished() == 0
    assert chat.deleted == []
    assert standing.notification(chat.chat_id, SESSION) is not None


@pytest.mark.asyncio
async def test_pressing_a_leftover_notification_keeps_the_standing_one() -> None:
    """The chat outlives the record of it, so the button pressed is not always the current one.

    Observed on 2026-08-21: a notification sent before this table existed was still in the
    chat when the deploy restarted the service, and the next report quite correctly started a
    fresh message beside it. Pressing the leftover's button discards *that* message — right —
    and used to forget the session's standing record with it, so the report after that started
    a third message while the second stood. Each press would have cost one more.
    """
    chat, standing, callbacks = _Chat(), StandingNotificationStore(), CallbackStateStore()
    notifier = _notifier(chat, standing, callbacks=callbacks)
    await notifier.deliver([_activity()])
    current = standing.notification(chat.chat_id, SESSION)

    notifier.forget(SESSION, current.message_id - 2)

    kept = standing.notification(chat.chat_id, SESSION)
    assert kept is not None and kept.message_id == current.message_id


@pytest.mark.asyncio
async def test_pressing_the_current_notification_still_forgets_it() -> None:
    """The behaviour the guard above must not cost: the message is gone, so the record goes."""
    chat, standing, callbacks = _Chat(), StandingNotificationStore(), CallbackStateStore()
    notifier = _notifier(chat, standing, callbacks=callbacks)
    await notifier.deliver([_activity()])

    notifier.forget(SESSION, standing.notification(chat.chat_id, SESSION).message_id)

    assert standing.notification(chat.chat_id, SESSION) is None
