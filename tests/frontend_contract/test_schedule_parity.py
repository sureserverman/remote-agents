"""Every fact a schedule's line carries is drawn by both surfaces (DEC-091).

One schedule list is handed to both: the bot's Schedules screen and the terminal's project
schedules. The facts -- agent, project, repeat words, next fire or `paused` -- and the message's
start are read off `session_views.schedule_lines`, the one function both call, and each is
asked of each surface. The wording around them stays each surface's own (DEC-043).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, time
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from backends import SessionUseCaseDouble, backend_for, tui_context_for
from fake_telegram import FakeChat
from schedule_fakes import MemoryScheduleStore
from textual.widgets import OptionList

from remote_agents.adapters.telegram.presenters import unpadded
from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.adapters.tui.panes import ProjectsPane
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.schedule_book import ScheduleBook
from remote_agents.application.schedule_times import next_fire
from remote_agents.application.session_views import ScheduleLine, schedule_lines
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.provider_descriptor import ComposerScreen
from remote_agents.ports.schedules import Once, Repeat, Schedule, Weekday

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)  # Friday, 12:00 in Berlin
PROJECT = CatalogProject("opaque-infra", "remote-agents", "infra", "Registered")
SRC = Path(__file__).resolve().parents[2] / "src" / "remote_agents"


def _schedule(
    schedule_id: str, when, *, paused: bool = False, prompt: str = "hi", project: str = ""
) -> Schedule:
    return Schedule(
        id=schedule_id,
        project_id=ProjectId(project or PROJECT.opaque_id),
        profile_id=ProfileId("claude"),
        prompt=prompt,
        when=when,
        paused=paused,
        next_fire_at=None if paused else next_fire(when, NOW, BERLIN),
        created_at=NOW.replace(minute=int(schedule_id[-1])),
    )


def _book(store: MemoryScheduleStore) -> ScheduleBook:
    return ScheduleBook(
        store,
        composers={"claude": ComposerScreen(composer=r"(?P<draft>.*)\Z")},
        projects=lambda: (ProjectId(PROJECT.opaque_id),),
        zone=lambda: BERLIN,
        now=lambda: NOW,
    )


SCHEDULES = (
    _schedule("s1", Repeat.weekdays(time(9, 0)), prompt="summarise the open pull requests today"),
    _schedule("s2", Repeat.daily(time(3, 0)), prompt="run the nightly checks and report"),
    _schedule("s3", Repeat(frozenset({Weekday.MON, Weekday.THU}), time(18, 30))),
    _schedule("s4", Once(datetime(2026, 10, 20, 7, 15)), prompt="a" * 60),
    _schedule("s5", Repeat.weekdays(time(12, 0)), paused=True),
    # A project that has left the catalogue: both surfaces still list it, under its id.
    _schedule("s6", Repeat.daily(time(6, 0)), project="opaque-gone"),
)


class _Launcher(SessionUseCaseDouble):
    async def list_sessions(self):
        return []

    async def project_usage(self):
        return ()

    async def refresh_readiness(self) -> None:
        return None


class _Creator:
    def available_areas(self) -> tuple[str, ...]:
        return ("infra",)


async def _expected() -> tuple[ScheduleLine, ...]:
    listed = await _book(MemoryScheduleStore(*SCHEDULES)).list()
    return schedule_lines(listed, (PROJECT,), BERLIN, NOW)


async def _bot_text() -> str:
    store = MemoryScheduleStore(*SCHEDULES)
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(catalogue=(PROJECT,), sessions=_Launcher(), schedules=_book(store)),
    )
    chat = FakeChat()
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    keyboard = chat.messages[anchor].reply_markup.inline_keyboard
    token = next(
        button.callback_data
        for row in keyboard
        for button in row
        if "Schedules" in unpadded(button.text)
    )
    await boundary.callback(chat.press(token), None)
    return unescape(chat.messages[anchor].text)


async def _terminal_rows() -> list[str]:
    store = MemoryScheduleStore(*SCHEDULES)
    app = ProjectsPane(
        tui_context_for(
            sessions=_Launcher(),
            projects=_Creator(),
            profiles=(ProfileAvailability("claude", True),),
            refresh_catalogue=lambda: (PROJECT,),
            catalogue=(PROJECT,),
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
            schedules=_book(store),
        )
    )
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await app.screen.choose(PROJECT.opaque_id)
        await pilot.pause()
        await app.screen.choose("schedules")
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        return [str(choices.get_option_at_index(i).prompt) for i in range(choices.option_count)]


async def test_the_expected_lines_cover_every_kind_of_fact() -> None:
    """The fixture is the premise: each repeat shape, a one-shot, a paused one, a cut message."""
    expected = await _expected()
    facts = " | ".join(line.facts for line in expected)
    for words in (
        "weekdays 09:00",
        "daily 03:00",
        "Mon Thu 18:30",
        "once 20 Oct 07:15",
        "· paused",
        "next Mon 09:00",
        "next tomorrow 03:00",
    ):
        assert words in facts, words
    assert any(line.preview.endswith("…") for line in expected)


@pytest.mark.parametrize("index", range(len(SCHEDULES)))
async def test_both_surfaces_draw_every_fact_of_each_schedule(index: int) -> None:
    line = (await _expected())[index]
    bot = await _bot_text()
    terminal = await _terminal_rows()

    assert line.facts in bot, (line.facts, bot)
    assert line.preview in bot, (line.preview, bot)
    assert any(line.facts in row and line.preview in row for row in terminal), (line, terminal)


def test_both_surfaces_ask_the_one_line_function() -> None:
    """Neither surface composes a schedule's listed line for itself (DEC-091)."""
    for path in (
        SRC / "adapters" / "telegram" / "service.py",
        SRC / "adapters" / "tui" / "screens" / "schedule.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        called = {
            getattr(node.func, "id", getattr(node.func, "attr", None))
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        assert "schedule_lines" in called, path
