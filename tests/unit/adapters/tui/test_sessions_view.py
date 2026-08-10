"""The local surface can see every managed session, not only the one it launched."""

from __future__ import annotations

import asyncio
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
    #: Makes the store read slow enough for a navigation or a second read to interleave with
    #: it, which is the only way the races in this file are reproducible at all.
    read_delay: float = 0.0

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        self.refreshed += 1
        if self.read_delay:
            await asyncio.sleep(self.read_delay)
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
    assert rows == ["No managed sessions on this host."], (
        "the pane was blank; the status line alone is not an empty state, because the region "
        "the owner is reading is the list"
    )


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


async def test_the_list_re_reads_itself_on_an_interval() -> None:
    """The one position whose answer goes stale with nobody touching it.

    The store has a second writer — the bot, and any reconcile the host runs — so a session
    can appear or end while the owner sits here reading. Driven by advancing the interval
    rather than by waiting for it: the timer is asked to fire, and what is asserted is that
    firing it re-reads the store and redraws.
    """
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        assert _rows(app) == [_rows(app)[0]]
        reads_after_open = launcher.refreshed

        launcher.records = (_record(), _record())
        await app.screen._auto_reload()
        await pilot.pause()
        rows_after_tick = _rows(app)
        reads_after_tick = launcher.refreshed

    assert reads_after_tick > reads_after_open, "the interval did not re-read the store"
    assert len(rows_after_tick) == 2, "a session another process started never appeared"


async def test_the_interval_is_paused_while_another_screen_is_on_top() -> None:
    """A paused timer is the point: `load_sessions` probes tmux, so an unpaused one would keep
    a background conversation with the runtime going underneath every screen pushed on this."""
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        screen = app.screen
        assert screen._auto is not None, "no interval was started"
        # `Timer.pause()` clears `_active` and `resume()` sets it; there is no public
        # predicate for "is this timer running", so the flag it actually gates on is what is
        # read. Asserted at all three points rather than only the paused one, because a timer
        # that was never running would satisfy the middle assertion on its own.
        running_here = screen._auto._active.is_set()

        await app.show_detail(str(launcher.records[0].session_id))
        await pilot.pause()
        paused_under_detail = screen._auto._active.is_set()

        await app.action_back()
        await pilot.pause()
        resumed_on_return = screen._auto._active.is_set()

    assert running_here, "the interval was not running on the screen that owns it"
    assert not paused_under_detail, "the interval kept polling under the detail screen"
    assert resumed_on_return, "the interval did not resume when the screen came back"


async def test_the_background_read_leaves_the_cursor_where_the_owner_put_it() -> None:
    """A refill that walks the selection back to row 0 every ten seconds is worse than stale.

    On the tick the owner presses enter, it would open a different session's detail than the
    one they were looking at — and one screen deeper are the destructive actions.
    """
    first, second, third = _record(), _record(), _record()
    launcher = _Listing((first, second, third))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.highlighted = 2
        chosen = choices.get_option_at_index(2).id

        await app.screen._auto_reload()
        await pilot.pause()
        still_on = app.screen.query_one("#choices", OptionList)
        resting_id = still_on.get_option_at_index(still_on.highlighted).id

    assert resting_id == chosen, "the background re-read moved the owner's selection"


async def test_a_session_ending_above_the_cursor_does_not_shift_the_selection() -> None:
    """Restored by row key rather than by index, which is what makes that true.

    A session that ends between two ticks shortens the list above the cursor, so the index
    the owner was on now names a different session.
    """
    first, second, third = _record(), _record(), _record()
    launcher = _Listing((first, second, third))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.highlighted = 2
        chosen = choices.get_option_at_index(2).id

        launcher.records = (second, third)
        await app.screen._auto_reload()
        await pilot.pause()
        after = app.screen.query_one("#choices", OptionList)
        resting_id = after.get_option_at_index(after.highlighted).id

    assert resting_id == chosen, "the cursor followed the index instead of the session"


async def test_a_selection_that_ended_falls_back_to_the_first_row() -> None:
    """DEC-007's resting rule: a fill always lands on a non-mutating entry, never nowhere."""
    first, second = _record(), _record()
    launcher = _Listing((first, second))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.highlighted = 1

        launcher.records = (first,)
        await app.screen._auto_reload()
        await pilot.pause()
        after = app.screen.query_one("#choices", OptionList)
        resting = after.highlighted

    assert resting == 0


async def test_the_background_read_is_silent_about_a_failure_the_owner_did_not_ask_for() -> None:
    """Ctrl+R still reports loudly — that read *was* asked for. A timer must not toast on a loop."""
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()

        launcher.list_error = RuntimeError("database is locked")
        await app.screen._auto_reload()
        await pilot.pause()
        quiet = announcements(app, severity="error")

        await app.screen.refresh_contents()
        await pilot.pause()
        loud = announcements(app, severity="error")

    assert quiet == [], f"the background read announced a failure nobody asked about: {quiet}"
    assert any("could not be read" in message for message in loud), loud


async def test_the_background_read_stands_down_while_a_command_is_in_flight() -> None:
    """Re-listing under a held guard would repaint the rows a confirmation is reasoning about."""
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        before = launcher.refreshed

        app._busy = True
        await app.screen._auto_reload()
        await pilot.pause()
        during = launcher.refreshed
        app._busy = False

    assert during == before, "the interval read the store while a command was in flight"


async def test_the_background_read_does_not_draw_onto_its_own_corpse() -> None:
    """The same shape as the keyed-read case above, pointed at the interval.

    That test exists because this defect class was already known here; this one exists
    because the new method reintroduced it and nothing pointed at it. `_auto_reload` checks
    `showing` *before* awaiting the store, so a screen popped during a slow read reached
    `_draw_listing` with its widgets gone — and the `keep_cursor` branch dereferences
    `#choices` directly, so it raised `NoMatches`.

    Inside a `Timer` callback that is not a caught error: `Timer._tick` hands any exception to
    `App._handle_exception`, whose docstring reads "Always results in the app exiting". A
    refresh nobody asked for could take the surface down, and the window is widest exactly
    when the host is slow — which is when the auto-refresh earns its place.
    """
    launcher = _Listing((_record(), _record()), read_delay=0.03)
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        screen = app.screen

        reading = asyncio.create_task(screen._auto_reload())
        await asyncio.sleep(0.005)
        await app.action_back()
        await pilot.pause()

        # The assertion is that awaiting this raises nothing at all.
        await reading
        still_alive = app.screen.position

    assert still_alive == "PROJECTS"


async def test_a_tick_landing_mid_refresh_does_not_start_a_second_read() -> None:
    """Ctrl+R holds no busy guard, so `tui.busy` never protected this.

    Two concurrent `load_sessions` calls double the tmux readiness probe on a host already
    slow enough for them to overlap, and whichever draw lands last wins — silently discarding
    the manual refresh's own cursor reset.
    """
    launcher = _Listing((_record(),), read_delay=0.03)
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        screen = app.screen
        launcher.refreshed = 0

        manual = asyncio.create_task(screen.reload())
        await asyncio.sleep(0.005)
        await screen._auto_reload()
        await manual
        await pilot.pause()

    assert launcher.refreshed == 1, (
        f"the interval read the store underneath a manual refresh: {launcher.refreshed} reads"
    )


async def test_a_keyed_refresh_is_never_refused_because_a_tick_is_in_flight() -> None:
    """The flag is one-directional on purpose: the owner asking again always wins."""
    launcher = _Listing((_record(),), read_delay=0.02)
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        screen = app.screen
        launcher.refreshed = 0

        background = asyncio.create_task(screen._auto_reload())
        await asyncio.sleep(0.003)
        await screen.refresh_contents()
        await background
        await pilot.pause()

    assert launcher.refreshed == 2, "the owner's own Refresh was swallowed by a background tick"


async def test_the_interval_actually_fires_without_being_called_by_hand() -> None:
    """Everything else here drives `_auto_reload()` directly, which never proves it is wired.

    A wrong interval — a unit typo, an off-by-1000 — would pass every other test in this file
    while making the feature useless or punishing in production. This one lets the real
    `Timer` created by `set_interval` fire on its own clock, with the interval patched down so
    the test does not wait ten seconds for it.
    """
    import remote_agents.adapters.tui.screens.sessions as sessions_module

    original = sessions_module._SESSIONS_AUTO_REFRESH
    sessions_module._SESSIONS_AUTO_REFRESH = 0.05
    try:
        launcher = _Listing((_record(),))
        app = RemoteAgentsTui(_context(launcher))

        async with app.run_test() as pilot:
            await app.action_sessions()
            await pilot.pause()
            after_open = launcher.refreshed

            launcher.records = (_record(), _record())
            await pilot.pause(0.2)
            fired = launcher.refreshed
            rows = _rows(app)
    finally:
        sessions_module._SESSIONS_AUTO_REFRESH = original

    assert fired > after_open, "the scheduled callback never ran on its own"
    assert len(rows) == 2, "the interval fired but its result never reached the rows"


def test_the_configured_interval_is_a_sane_number_of_seconds() -> None:
    """Pins the unit. The test above patches the value, so nothing else would notice a typo."""
    from remote_agents.adapters.tui.screens.sessions import _SESSIONS_AUTO_REFRESH

    assert 2.0 <= _SESSIONS_AUTO_REFRESH <= 60.0, _SESSIONS_AUTO_REFRESH
