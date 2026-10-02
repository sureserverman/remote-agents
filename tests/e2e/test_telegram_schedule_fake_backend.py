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
