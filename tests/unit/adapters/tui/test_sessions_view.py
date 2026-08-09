"""The local surface can see every managed session, not only the one it launched."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from textual.widgets import OptionList
from tui_feedback import announcements
from tui_feedback import status as _status
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.screens import ProjectsScreen
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_EXISTING = CatalogProject("opaque-existing", "existing", "infra", "Registered")


def _record(state: SessionState = SessionState.RUNNING, *, minutes: int = 0) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        state,
        datetime.now(UTC) - timedelta(minutes=minutes),
    )


@dataclass(slots=True)
class _Listing:
    """A launcher that reports whatever set of sessions the test asked for."""

    records: tuple[SessionRecord, ...] = ()
    refreshed: int = 0
    list_error: Exception | None = None

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        self.refreshed += 1
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        if self.list_error is not None:
            raise self.list_error
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        return None

    async def launch(self, _command):  # pragma: no cover - wizard path, unused here
        raise AssertionError("the sessions view must not launch anything")


def _context(launcher: _Listing) -> TuiContext:
    return TuiContext(
        launcher=launcher,  # type: ignore[arg-type]
        creator=object(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_EXISTING,),
        attach_argv=lambda session_id: (
            "tmux",
            "-L",
            "remote-agents",
            "attach-session",
            "-t",
            f"={session_id}",
        ),
        catalogue=(_EXISTING,),
    )


def _rows(app: RemoteAgentsTui) -> list[str]:
    return [str(option.prompt) for option in app.screen.query_one("#choices", OptionList).options]


async def test_sessions_lists_one_row_per_managed_session() -> None:
    launcher = _Listing((_record(SessionState.RUNNING), _record(SessionState.PRESERVED)))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        rows = _rows(app)

    assert len(rows) == 2
    assert all("existing" in row for row in rows)


async def test_sessions_refreshes_readiness_before_listing() -> None:
    """A session that became ready elsewhere must not still read as failed here."""
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()

    assert launcher.refreshed == 1


async def test_each_row_names_the_project_state_and_age() -> None:
    launcher = _Listing((_record(SessionState.RUNNING, minutes=7),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        row = _rows(app)[0]

    assert "existing" in row
    assert "running" in row
    assert "7m" in row


async def test_ended_sessions_are_filtered_exactly_as_the_bot_filters_them() -> None:
    launcher = _Listing((_record(SessionState.ENDED), _record(SessionState.RUNNING)))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        rows = _rows(app)

    assert len(rows) == 1
    assert "running" in rows[0]


async def test_no_managed_session_renders_an_explicit_empty_state() -> None:
    app = RemoteAgentsTui(_context(_Listing(())))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        status = _status(app)
        rows = _rows(app)

    assert "no managed sessions" in status.casefold()
    assert rows == []


async def test_every_ended_list_still_renders_the_empty_state() -> None:
    """Filtering must not leave the owner staring at a list that claims sessions exist."""
    app = RemoteAgentsTui(_context(_Listing((_record(SessionState.ENDED),))))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        status = _status(app)

    assert "no managed sessions" in status.casefold()


async def test_a_store_error_reports_itself_and_leaves_the_wizard_reachable() -> None:
    launcher = _Listing((), list_error=RuntimeError("database is locked"))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        reported = announcements(app, severity="error")
        await app.action_back()
        await pilot.pause()
        step = position(app)

    assert any("could not be read" in message for message in reported), reported
    assert step == "PROJECTS"


async def test_the_sessions_step_does_not_disturb_a_launch_in_progress() -> None:
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        app._busy = True
        await app.action_sessions()
        await pilot.pause()
        step = position(app)

    assert step == "PROJECTS"
    assert launcher.refreshed == 0


@dataclass(slots=True)
class _FlakyListing:
    """Succeeds for the first read, then fails — a store contended by the other writer."""

    records: tuple[SessionRecord, ...] = ()
    reads: int = 0
    fail_reads: bool = True
    attach_error: Exception | None = None
    # How many reads succeed before the store starts failing. One is enough to open the
    # sessions list; a test that has to *navigate* somewhere before provoking the failure
    # says how many it needs rather than hard-coding a count into the fake.
    fail_after: int = 1

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        self.reads += 1
        if self.fail_reads and self.reads > self.fail_after:
            raise RuntimeError("database is locked")
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        if self.attach_error is not None:
            raise self.attach_error
        return None


async def test_a_store_error_opening_detail_is_reported_not_raised() -> None:
    """Recovery is exactly when the store is contended; the surface must survive it."""
    record = _record()
    app = RemoteAgentsTui(_context(_FlakyListing((record,))))  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        reported = announcements(app, severity="error")

    assert any("could not be read" in message for message in reported), reported


async def test_a_store_error_rendering_attach_is_reported_not_raised() -> None:
    """The read `show_attach` makes for itself can fail even once the detail is open.

    Two reads are allowed through so the owner genuinely reaches the detail — the list, then
    the detail's own re-read — and the store fails on the third, which is the one this method
    makes. The previous version of this test skipped the navigation by writing the session id
    onto the app; the id belongs to the screen now, so the test walks there instead, and that
    is a better exercise of the same guarantee rather than a workaround for the move.
    """
    record = _record()
    launcher = _FlakyListing((record,), fail_after=2)
    app = RemoteAgentsTui(_context(launcher))  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        assert not announcements(app, severity="error"), (
            "the detail must open cleanly, or this asserts on the wrong read"
        )

        await app.screen.show_attach()
        await pilot.pause()
        reported = announcements(app, severity="error")

    assert any("could not be read" in message for message in reported), reported


async def test_a_failing_copy_attach_is_reported_not_raised() -> None:
    """copy_attach re-reads the record and inspects the terminal; either can fail."""
    record = _record()
    launcher = _FlakyListing(
        (record,), fail_reads=False, attach_error=RuntimeError("terminal server is gone")
    )
    app = RemoteAgentsTui(_context(launcher))  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("attach")
        await pilot.pause()
        reported = announcements(app, severity="error")

    assert any("could not be read" in message for message in reported), reported


async def test_selecting_a_row_never_escapes_as_an_exception() -> None:
    """The keystroke path is what the owner actually uses; it must not tear down the app."""
    record = _record()
    app = RemoteAgentsTui(_context(_FlakyListing((record,))))  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        reported = announcements(app, severity="error")

    assert any("could not be read" in message for message in reported), reported


async def test_a_screen_left_mid_read_does_not_draw_onto_its_own_corpse() -> None:
    """The shared guard, pinned on a path that deliberately holds no busy guard.

    `SessionsScreen.reload` is one of several screen methods that await a store read and then
    draw, without blocking navigation while they do — reloading a list is not worth freezing
    the surface for. That makes it the right place to pin `ChoiceScreen.showing`, which is the
    *class* fix for this: a screen that has been left renders nothing, instead of calling
    `query_one` on widgets that are already unmounted and raising `NoMatches` out of a message
    handler, which exits the app.

    The distinction matters because the destructive paths close the same hole a second way, by
    holding the busy guard so the pop cannot happen at all. If this test were written against
    one of those it would pass with `showing` removed entirely — verified by mutation, which is
    why it is written here instead.
    """
    import asyncio

    @dataclass(slots=True)
    class _SlowListing:
        records: tuple[SessionRecord, ...] = ()

        async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
            return self.records

        async def list_sessions(self) -> tuple[SessionRecord, ...]:
            await asyncio.sleep(0.03)
            return self.records

        async def copy_attach(self, _session_id) -> str | None:
            return None

    app = RemoteAgentsTui(_context(_SlowListing((_record(),))))  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        sessions = app.screen

        async def _escape_during() -> None:
            await asyncio.sleep(0.005)
            await app.action_back()

        # The assertion is that this returns at all, and that the app survives it.
        await asyncio.gather(sessions.reload(), _escape_during())
        await pilot.pause()

        assert app.is_running, "a render onto a left screen took the app down"
        assert isinstance(app.screen, ProjectsScreen)
        assert len(app.screen_stack) == 1
