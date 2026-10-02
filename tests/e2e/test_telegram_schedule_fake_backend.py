"""Scheduled sessions on the bot: create (project → agent → time → repeat → message → confirm)
and manage (list, pause, resume, delete), over a real `ScheduleBook` and a fake chat."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest
from backends import SessionUseCaseDouble, backend_for
from fake_telegram import FakeChat
from schedule_fakes import MemoryScheduleStore

from remote_agents.adapters.telegram.presenters import unpadded
from remote_agents.adapters.telegram.service import PrivateBotBoundary, build_private_bot
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.schedule_book import ScheduleBook
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.provider_descriptor import ComposerScreen
from remote_agents.ports.schedules import Once, Repeat, Schedule

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)  # Friday, 12:00 in Berlin
PROJECT = "a" * 24
_COMPOSER = ComposerScreen(composer=r"(?P<draft>.*)\Z", command_menu=r"(?P<first>\S+)")


class _Launcher(SessionUseCaseDouble):
    async def list_sessions(self):
        return []

    async def refresh_readiness(self) -> None:
        return None


def _bot(store: MemoryScheduleStore | None = None) -> tuple[PrivateBotBoundary, ScheduleBook]:
    book = ScheduleBook(
        store if store is not None else MemoryScheduleStore(),
        composers={"claude": _COMPOSER, "codex": _COMPOSER},
        projects=lambda: (ProjectId(PROJECT),),
        zone=lambda: BERLIN,
        now=lambda: NOW,
    )
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(CatalogProject(PROJECT, "remote-agents", "infra", "Registered"),),
            sessions=_Launcher(),
            profiles=(ProfileAvailability("claude", True), ProfileAvailability("codex", True)),
            schedules=book,
        ),
        profiles=(ProfileAvailability("claude", True), ProfileAvailability("codex", True)),
    )
    return boundary, book


def _labels(message) -> list[str]:
    keyboard = getattr(message.reply_markup, "inline_keyboard", ())
    return [unpadded(button.text).removeprefix("• ") for row in keyboard for button in row]


def _button(message, label: str) -> str:
    keyboard = getattr(message.reply_markup, "inline_keyboard", ())
    buttons = [button for row in keyboard for button in row]
    for button in buttons:
        if unpadded(button.text).removeprefix("• ") == label:
            return button.callback_data
    for button in buttons:
        if label in unpadded(button.text):
            return button.callback_data
    raise AssertionError(f"no button {label!r} in {_labels(message)}")


async def _press(chat: FakeChat, boundary: PrivateBotBoundary, anchor: int, label: str) -> None:
    await boundary.callback(chat.press(_button(chat.messages[anchor], label)), None)


async def _to_time_step(chat: FakeChat, boundary: PrivateBotBoundary) -> int:
    await boundary.launch_command(chat.message_update("/launch"), None)
    anchor = chat.bot_messages[0].message_id
    await _press(chat, boundary, anchor, "remote-agents")
    await _press(chat, boundary, anchor, "Schedule")
    await _press(chat, boundary, anchor, "Claude")
    return anchor


@pytest.mark.asyncio
async def test_create_a_weekdays_nine_schedule_round_trips() -> None:
    boundary, book = _bot()
    chat = FakeChat()
    anchor = await _to_time_step(chat, boundary)

    await _press(chat, boundary, anchor, "tomorrow 09:00")
    await _press(chat, boundary, anchor, "Weekdays")
    await boundary.text(chat.message_update("summarise the open PRs"), None)
    summary = chat.messages[anchor].text
    assert "weekdays 09:00" in summary
    assert "Mon 09:00" in summary, "the next fire, in local time, before it is saved"
    assert await book.list() == ()
    await _press(chat, boundary, anchor, "Confirm")

    (saved,) = await book.list()
    assert saved.when == Repeat.weekdays(time(9, 0))
    assert saved.prompt == "summarise the open PRs"
    assert saved.profile_id == ProfileId("claude") and saved.project_id == ProjectId(PROJECT)
    assert "Scheduled" in chat.messages[anchor].text
    assert len(chat.bot_messages) == 1, chat.transcript()


@pytest.mark.asyncio
async def test_create_a_one_shot_from_a_typed_date() -> None:
    boundary, book = _bot()
    chat = FakeChat()
    anchor = await _to_time_step(chat, boundary)

    await _press(chat, boundary, anchor, "Type a time")
    await boundary.text(chat.message_update("2026-10-03 03:00"), None)
    await _press(chat, boundary, anchor, "Once")
    await boundary.text(chat.message_update("reply with OK"), None)
    await _press(chat, boundary, anchor, "Confirm")

    (saved,) = await book.list()
    assert saved.when == Once(datetime(2026, 10, 3, 3, 0))


@pytest.mark.asyncio
async def test_create_a_shell_message_is_refused_in_words_and_stays_on_the_message_step() -> None:
    boundary, book = _bot()
    chat = FakeChat()
    anchor = await _to_time_step(chat, boundary)
    await _press(chat, boundary, anchor, "tomorrow 09:00")
    await _press(chat, boundary, anchor, "Daily")

    await boundary.text(chat.message_update("!ls"), None)

    box = [message for message in chat.bot_messages if message.message_id != anchor]
    assert len(box) == 1, chat.transcript()
    assert "shell command" in box[0].text
    await boundary.text(chat.message_update("list the files"), None)
    await _press(chat, boundary, anchor, "Confirm")
    (saved,) = await book.list()
    assert saved.prompt == "list the files"


@pytest.mark.asyncio
async def test_create_a_typed_time_that_is_not_one_is_refused_in_words() -> None:
    boundary, _ = _bot()
    chat = FakeChat()
    anchor = await _to_time_step(chat, boundary)
    await _press(chat, boundary, anchor, "Type a time")

    await boundary.text(chat.message_update("25:00"), None)

    box = [message for message in chat.bot_messages if message.message_id != anchor]
    assert len(box) == 1, chat.transcript()
    assert "HH:MM" in box[0].text


@pytest.mark.asyncio
async def test_create_a_one_shot_in_the_past_is_refused_at_confirm() -> None:
    boundary, book = _bot()
    chat = FakeChat()
    anchor = await _to_time_step(chat, boundary)
    await _press(chat, boundary, anchor, "Type a time")
    await boundary.text(chat.message_update("2026-10-01 09:00"), None)
    await _press(chat, boundary, anchor, "Once")
    await boundary.text(chat.message_update("hi"), None)
    await _press(chat, boundary, anchor, "Confirm")

    assert await book.list() == ()
    assert "already passed" in chat.messages[anchor].text


@pytest.mark.asyncio
async def test_create_chosen_days_repeat_on_those_days() -> None:
    boundary, book = _bot()
    chat = FakeChat()
    anchor = await _to_time_step(chat, boundary)
    await _press(chat, boundary, anchor, "tomorrow 09:00")
    await _press(chat, boundary, anchor, "Pick days")
    await _press(chat, boundary, anchor, "Mon")
    await _press(chat, boundary, anchor, "Thu")
    await _press(chat, boundary, anchor, "Done")
    await boundary.text(chat.message_update("weekly digest"), None)
    await _press(chat, boundary, anchor, "Confirm")

    (saved,) = await book.list()
    assert saved.when == Repeat(frozenset({0, 3}), time(9, 0))


@pytest.mark.asyncio
async def test_create_offers_no_schedule_where_the_host_manages_none() -> None:
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(CatalogProject(PROJECT, "remote-agents", "infra", "Registered"),),
            sessions=_Launcher(),
            profiles=(ProfileAvailability("claude", True),),
        ),
        profiles=(ProfileAvailability("claude", True),),
    )
    chat = FakeChat()
    await boundary.launch_command(chat.message_update("/launch"), None)
    anchor = chat.bot_messages[0].message_id
    await _press(chat, boundary, anchor, "remote-agents")

    assert not any("Schedule" in label for label in _labels(chat.messages[anchor]))


def _saved(schedule_id: str, *, paused: bool = False, hour: int = 9) -> Schedule:
    return Schedule(
        id=schedule_id,
        project_id=ProjectId(PROJECT),
        profile_id=ProfileId("claude"),
        prompt="summarise the open pull requests and say which ones need me today",
        when=Repeat.weekdays(time(hour, 0)),
        paused=paused,
        next_fire_at=None if paused else datetime(2026, 10, 5, hour - 2, 0, tzinfo=UTC),
        created_at=NOW,
    )


async def _open_schedules(chat: FakeChat, boundary: PrivateBotBoundary) -> int:
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    await _press(chat, boundary, anchor, "Schedules")
    return anchor


@pytest.mark.asyncio
async def test_manage_lists_each_schedule_with_its_facts_and_its_message() -> None:
    store = MemoryScheduleStore(_saved("s1"), _saved("s2", paused=True, hour=18))
    boundary, _ = _bot(store)
    chat = FakeChat()

    anchor = await _open_schedules(chat, boundary)

    text = chat.messages[anchor].text
    assert "claude · remote-agents · weekdays 09:00 · next Mon 09:00" in text
    assert "claude · remote-agents · weekdays 18:00 · paused" in text
    assert "summarise the open pull requests and sa…" in text
    labels = _labels(chat.messages[anchor])
    assert "⏸ 1" in labels and "▶️ 2" in labels and "🗑 1" in labels


@pytest.mark.asyncio
async def test_manage_pause_shows_paused_and_drops_it_from_due() -> None:
    store = MemoryScheduleStore(_saved("s1"))
    boundary, book = _bot(store)
    chat = FakeChat()
    anchor = await _open_schedules(chat, boundary)

    await _press(chat, boundary, anchor, "⏸ 1")

    assert "· paused" in chat.messages[anchor].text
    assert await store.due(datetime(2026, 10, 9, tzinfo=UTC)) == ()
    await _press(chat, boundary, anchor, "▶️ 1")
    assert "next Mon 09:00" in chat.messages[anchor].text
    assert len(await store.due(datetime(2026, 10, 9, tzinfo=UTC))) == 1


@pytest.mark.asyncio
async def test_manage_delete_asks_first_and_removes_the_row_after_confirm() -> None:
    store = MemoryScheduleStore(_saved("s1"))
    boundary, book = _bot(store)
    chat = FakeChat()
    anchor = await _open_schedules(chat, boundary)

    await _press(chat, boundary, anchor, "🗑 1")
    assert "Delete this schedule?" in chat.messages[anchor].text
    assert len(await book.list()) == 1, "nothing is deleted before the confirmation"
    await _press(chat, boundary, anchor, "🗑 Delete")

    assert await book.list() == ()
    assert "No schedules" in chat.messages[anchor].text


@pytest.mark.asyncio
async def test_manage_a_schedule_added_from_another_process_appears_after_the_watcher_fires() -> (
    None
):
    store = MemoryScheduleStore()
    boundary, _ = _bot(store)
    chat = FakeChat()
    anchor = await _open_schedules(chat, boundary)
    assert "No schedules" in chat.messages[anchor].text

    await store.add(_saved("s9"))  # the terminal surface's write, through the shared store
    drew = await boundary.redraw_schedules_if_open(chat.bot)

    assert drew is True
    assert "weekdays 09:00 · next Mon 09:00" in chat.messages[anchor].text
    assert await boundary.redraw_schedules_if_open(chat.bot) is False, "nothing new, no edit"


@pytest.mark.asyncio
async def test_manage_the_watcher_leaves_any_other_screen_alone() -> None:
    store = MemoryScheduleStore()
    boundary, _ = _bot(store)
    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)

    await store.add(_saved("s9"))

    assert await boundary.redraw_schedules_if_open(chat.bot) is False


@pytest.mark.asyncio
async def test_manage_no_schedules_entry_where_the_host_manages_none() -> None:
    boundary = build_private_bot(7, 11, backend=backend_for(sessions=_Launcher()))
    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)

    assert not any("Schedules" in label for label in _labels(chat.bot_messages[0]))
