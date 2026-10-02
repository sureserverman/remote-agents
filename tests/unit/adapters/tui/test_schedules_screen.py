"""Scheduled sessions on the local surface: made from a project's chooser, one step at a time,
and listed there with pause, resume and delete -- the same facts the bot shows (DEC-091)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from backends import SessionUseCaseDouble, tui_context_for
from schedule_fakes import MemoryScheduleStore
from textual.widgets import Input, OptionList, Static
from tui_positions import position

from remote_agents.adapters.tui.panes import ProjectsPane
from remote_agents.adapters.tui.screens.dashboard import ProjectChooserScreen
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.schedule_book import ScheduleBook
from remote_agents.domain.models import ProjectId
from remote_agents.ports.provider_descriptor import ComposerScreen
from remote_agents.ports.schedules import Repeat

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)  # Friday, 12:00 in Berlin
_INFRA = CatalogProject("opaque-infra", "remote-agents", "infra", "Registered")
_COMPOSER = ComposerScreen(composer=r"(?P<draft>.*)\Z", command_menu=r"(?P<first>\S+)")


class _Creator:
    def available_areas(self) -> tuple[str, ...]:
        return ("infra",)


class _Launcher(SessionUseCaseDouble):
    async def project_usage(self):
        return ()

    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self):
        return ()


def _app(store: MemoryScheduleStore) -> tuple[ProjectsPane, ScheduleBook]:
    book = ScheduleBook(
        store,
        composers={"claude": _COMPOSER},
        projects=lambda: (ProjectId("opaque-infra"),),
        zone=lambda: BERLIN,
        now=lambda: NOW,
    )
    context = tui_context_for(
        sessions=_Launcher(),
        projects=_Creator(),
        profiles=(ProfileAvailability("claude", True),),
        refresh_catalogue=lambda: (_INFRA,),
        catalogue=(_INFRA,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        schedules=book,
    )
    return ProjectsPane(context), book


def _status(app) -> str:
    status = app.screen.query_one("#status", Static)
    return "".join(status.render_line(row).text for row in range(status.size.height))


def _rows(app) -> list[str]:
    choices = app.screen.query_one("#choices", OptionList)
    return [str(choices.get_option_at_index(i).prompt) for i in range(choices.option_count)]


async def _to_message_step(app, pilot) -> None:
    await app.screen.choose("opaque-infra")
    await pilot.pause()
    await app.screen.choose("schedule")
    await pilot.pause()
    assert position(app) == "SCHEDULE"
    await app.screen.choose("claude")
    await pilot.pause()
    await app.screen.choose("tonight_0300")
    await pilot.pause()
    await app.screen.choose("daily")
    await pilot.pause()


async def test_create_list_pause_and_delete_a_daily_three_am_schedule() -> None:
    store = MemoryScheduleStore()
    app, book = _app(store)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _to_message_step(app, pilot)
        assert app.screen.query_one("#filter", Input).display
        app.screen.submit("reply with OK")
        await pilot.pause()
        assert "daily 03:00" in _status(app) or any("daily 03:00" in row for row in _rows(app))
        await app.screen.choose("confirm")
        await pilot.pause()

        (saved,) = await book.list()
        assert saved.when == Repeat.daily(time(3, 0))
        assert saved.prompt == "reply with OK"
        assert isinstance(app.screen, ProjectChooserScreen), "back on the project's chooser"

        await app.screen.choose("schedules")
        await pilot.pause()
        assert position(app) == "SCHEDULES"
        assert any(
            "claude · remote-agents · daily 03:00 · next tomorrow 03:00" in row
            for row in _rows(app)
        ), _rows(app)

        await app.screen.choose(saved.id)
        await pilot.pause()
        await app.screen.choose("pause")
        await pilot.pause()
        assert any("daily 03:00 · paused" in row for row in _rows(app)), _rows(app)
        assert (await store.get(saved.id)).paused

        await app.screen.choose(saved.id)
        await pilot.pause()
        asking = asyncio.create_task(app.screen.choose("delete"))
        await pilot.pause()
        assert position(app) == "SCHEDULE_DELETE_MODAL"
        assert await book.list() != (), "nothing is deleted before the confirmation"
        await pilot.press("down")
        await pilot.press("enter")
        await asyncio.wait_for(asking, timeout=5)
        await pilot.pause()

        assert await book.list() == ()
        assert position(app) == "SCHEDULES"


async def test_a_shell_message_shows_its_refusal_under_the_field_and_stays() -> None:
    store = MemoryScheduleStore()
    app, book = _app(store)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _to_message_step(app, pilot)

        app.screen.submit("!ls")
        await pilot.pause()

        assert "shell command" in _status(app)
        assert app.screen.query_one("#filter", Input).display, "still on the message step"
        assert await book.list() == ()


async def test_a_typed_time_that_is_not_one_is_refused_in_words() -> None:
    store = MemoryScheduleStore()
    app, _ = _app(store)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await app.screen.choose("opaque-infra")
        await pilot.pause()
        await app.screen.choose("schedule")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        await app.screen.choose("type")
        await pilot.pause()

        app.screen.submit("25:00")
        await pilot.pause()

        assert "HH:MM" in _status(app)


async def test_the_chooser_offers_no_schedules_where_the_host_manages_none() -> None:
    context = tui_context_for(
        sessions=_Launcher(),
        projects=_Creator(),
        profiles=(ProfileAvailability("claude", True),),
        refresh_catalogue=lambda: (_INFRA,),
        catalogue=(_INFRA,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )
    app = ProjectsPane(context)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await app.screen.choose("opaque-infra")
        await pilot.pause()
        assert not any("chedule" in row for row in _rows(app))


async def test_escape_steps_back_one_question_and_keeps_the_choices() -> None:
    store = MemoryScheduleStore()
    app, _ = _app(store)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _to_message_step(app, pilot)

        await pilot.press("escape")
        await pilot.pause()

        assert position(app) == "SCHEDULE"
        assert "How often" in _status(app), "back at the repeat, with the time kept"
        await pilot.press("escape")
        await pilot.press("escape")
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ProjectChooserScreen)


async def test_the_list_shows_every_schedule_and_says_when_one_vanished() -> None:
    from remote_agents.domain.models import ProfileId
    from remote_agents.ports.schedules import Schedule

    other = Schedule(
        id="far1",
        project_id=ProjectId("opaque-gone"),
        profile_id=ProfileId("claude"),
        prompt="from a project that left the catalogue",
        when=Repeat.daily(time(9, 0)),
        paused=False,
        next_fire_at=datetime(2026, 10, 3, 7, 0, tzinfo=UTC),
        created_at=NOW,
    )
    store = MemoryScheduleStore(other)
    app, _ = _app(store)
    async with app.run_test(size=(160, 30)) as pilot:
        await pilot.pause()
        await app.screen.choose("opaque-infra")
        await pilot.pause()
        await app.screen.choose("schedules")
        await pilot.pause()
        assert any("opaque-gone · daily 09:00" in row for row in _rows(app)), _rows(app)

        await app.screen.choose("far1")
        await pilot.pause()
        await store.delete("far1")  # the bot deleted it meanwhile
        await app.screen.choose("pause")
        await pilot.pause()
        assert "no longer there" in _status(app)
