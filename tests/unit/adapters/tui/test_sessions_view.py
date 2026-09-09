"""The local surface can see every managed session, not only the one it launched."""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backends import SessionUseCaseDouble, backend_for
from textual.widgets import OptionList
from tui_feedback import announcements
from tui_feedback import status as _status
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.screens.launch import ProjectsScreen
from remote_agents.adapters.tui.screens.sessions import SessionsScreen
from remote_agents.application.profiles import ProfileAvailability
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
class _Listing(SessionUseCaseDouble):
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
        backend=backend_for(
            sessions=launcher,  # type: ignore[arg-type]
            projects=object(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_EXISTING,),
            catalogue=(_EXISTING,),
        ),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: (
            "tmux",
            "-L",
            "remote-agents",
            "attach-session",
            "-t",
            f"={session_id}",
        ),
    )


def _context_with_usage(launcher: _Listing) -> TuiContext:
    """Like `_context`, but with a usage port, which the gauge seed returns early without.

    `_seed_context_gauges` opens with `if self.tui.services.backend.usage is None: return`, so
    against this module's usage-less default context every assertion about it passes by never
    running. Both seed tests below were written that way first and both were green for that
    reason; the reader is the difference between testing the seed and testing the early exit.
    """

    async def _reading(session_id):
        return None

    return TuiContext(
        backend=backend_for(
            sessions=launcher,  # type: ignore[arg-type]
            projects=object(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_EXISTING,),
            catalogue=(_EXISTING,),
            usage=_reading,
        ),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
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
        await pilot.pause()
        before = launcher.refreshed  # the dashboard's own mount reload is the baseline
        await app.action_sessions()
        await pilot.pause()

    assert launcher.refreshed == before + 1


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
    assert step == "DASHBOARD"


async def test_the_sessions_step_does_not_disturb_a_launch_in_progress() -> None:
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await pilot.pause()
        before = launcher.refreshed  # the dashboard's own mount reload is the baseline
        app._busy = True
        await app.action_sessions()
        await pilot.pause()
        step = position(app)

    assert step == "DASHBOARD"
    assert launcher.refreshed == before


@dataclass(slots=True)
class _FlakyListing(SessionUseCaseDouble):
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
    launcher = _FlakyListing((record,), fail_after=3)  # +1: the dashboard's own mount read
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

    The distinction matters because the stop paths close the same hole a second way, by
    holding the busy guard so the pop cannot happen at all. If this test were written against
    one of those it would pass with `showing` removed entirely — verified by mutation, which is
    why it is written here instead.
    """
    import asyncio

    @dataclass(slots=True)
    class _SlowListing(SessionUseCaseDouble):
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
    one they were looking at — and one screen deeper are the stop actions.
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


async def test_a_vanished_row_leaves_the_cursor_on_nothing() -> None:
    """The mitigation the whole of DEC-052's amendment rests on.

    This used to assert the opposite -- `resting == 0`, on DEC-007's rule that a fill always
    lands on a non-mutating entry and never nowhere. That was defensible while every mutating
    action was two keypresses away behind `d`. It stopped being defensible when `s` and `c`
    were bound: row 0 of a list that has just lost a row is a *different session*, chosen by a
    ten-second timer rather than by the owner, and one keypress there gracefully stops a live
    agent with nothing asked and nothing recoverable. DEC-052 named that as the hazard that
    barred the keys, and listed this exact repair as its rejected alternative 3.

    DEC-007 is honoured rather than traded: its rule is about where a *resting* cursor may
    rest, and no cursor at all satisfies it strictly. The keys check `highlighted_session()`
    and return early on None, which the assertions below drive rather than assume.

    One arrow press brings the cursor back. That is the point -- the owner choosing a row
    again is the deliberate act the vanished row can no longer stand in for.
    """
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
        assert after.highlighted is None, (
            "the cursor was moved onto a session the owner never selected"
        )
        # Not merely undrawn: the keys ask this, and this is what refuses them.
        assert app.screen.highlighted_session() is None
        assert app.screen.check_action("row_action", ("graceful",)) is False

        # Still focused, or the arrow press that restores the cursor would go nowhere.
        assert after.has_focus, "the list stopped answering the keyboard"
        await pilot.press("down")
        await pilot.pause()
        assert app.screen.highlighted_session() is not None, "the owner cannot get a cursor back"


async def test_a_vanished_row_clears_the_cursor_even_on_an_unfocused_list() -> None:
    """The mitigation may not depend on where the keyboard is.

    `_draw_listing` passes `focus=choices.has_focus`, so an unfocused list takes the branch
    that does not re-place the cursor -- and `clear_options` leaves `highlighted` set while
    `validate_highlighted` clamps rather than rejects. A fill that skipped the clear because
    the keyboard was elsewhere would therefore leave the old index pointing at whatever row now
    sits there: the silent move, arrived at by the other road. Found by re-reading the branch
    rather than by a failing test, which is why it is pinned here.
    """
    first, second = _record(), _record()
    launcher = _Listing((first, second))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.highlighted = 1
        choices.blur()
        await pilot.pause()
        assert not choices.has_focus, "the list kept the keyboard, so this drives the wrong branch"

        launcher.records = (first,)
        await app.screen._auto_reload()
        await pilot.pause()
        after = app.screen.query_one("#choices", OptionList)

    assert after.highlighted is None, "an unfocused list kept a cursor on a vanished row"


async def test_a_surviving_row_still_keeps_the_cursor_after_a_background_read() -> None:
    """The other half, because a rule that only ever cleared would be indistinguishable from
    a list that had simply stopped restoring anything -- and the restore is what
    `test_a_session_ending_above_the_cursor_does_not_shift_the_selection` is about."""
    first, second = _record(), _record()
    launcher = _Listing((first, second))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.highlighted = 1
        chosen = choices.get_option_at_index(1).id

        await app.screen._auto_reload()
        await pilot.pause()
        after = app.screen.query_one("#choices", OptionList)

    assert after.highlighted is not None, "a surviving row lost its cursor"
    assert after.get_option_at_index(after.highlighted).id == chosen


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

    assert still_alive == "DASHBOARD"


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
            # A bounded poll, not a fixed wait. A single `pause(0.2)` against a 0.05s interval
            # is a 4x margin, and this project has already been bitten twice by exactly that
            # shape — a margin that holds on an idle machine and fails under load. The ceiling
            # is generous because it is only reached on failure; the loop exits as soon as the
            # timer has fired *and* its result has reached the rows.
            deadline = 100
            while deadline and (launcher.refreshed <= after_open or len(_rows(app)) != 2):
                await pilot.pause(0.02)
                deadline -= 1
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


async def test_a_keyed_refresh_keeps_the_cursor_on_the_row_the_owner_chose() -> None:
    """Ctrl+R re-reads the list; it does not re-choose the row.

    The fifth exit in the class `on_reveal`'s docstring enumerates. `refresh_contents` reloaded
    with `reload()`'s default `keep_cursor=False`, so `_draw_listing` took the
    `show_choices(rows)` branch and rested the cursor on row 0 — and `Ctrl+R` then `s` issued a
    graceful stop against a session the owner never selected. The four exits named there were
    each found this way, one at a time; this is the same defect at the one mouth that was not
    reached by moving the fix to the `on_reveal` funnel, because Refresh does not come through
    `on_reveal`.

    Driven through the real key rather than by calling `refresh_contents`, because the binding
    is the app's (`app.py`, `ctrl+r` -> `action_refresh`) and the claim under test is about what
    the owner's keypress does, not about what one method does when called by hand.
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

        await pilot.press("ctrl+r")
        await pilot.pause()

        after = app.screen.query_one("#choices", OptionList)
        assert after.highlighted is not None, "the refresh left the list with no cursor at all"
        resting_id = after.get_option_at_index(after.highlighted).id

    assert resting_id == chosen, "Ctrl+R re-chose the row instead of re-reading the list"


async def test_a_resize_re_lays_the_columns_without_clearing_the_list() -> None:
    """A width change re-columns the rows it already has; it does not refill the list.

    `on_resize` reran the whole clear/refill through `_draw_listing`, and in the console that
    is every DEC-040 exchange and every drag. Each one bumped `_resting_generation` and
    scheduled a fresh `_rest_cursor`, so every resize re-armed the window mechanism A lives in
    — the arrow that lands between a fill and its deferred placement. Narrowing this is what
    stops a resize being a cursor event at all.

    The identity assertion is the one that matters, and it is not decoration. `show_choices`
    clears and refills, so a row that survives a re-render is a *different* `Option` object
    with the same id — which is the predicate DEC-069 drops a queued selection on. Re-laying in
    place keeps the object, so a selection queued across a resize is now honoured rather than
    dropped. That is the intended reading (a resize changes no rows, so nothing the owner
    aimed at has moved), and it is asserted here so the narrowing is deliberate and visible
    rather than a side effect noticed later.
    """
    first, second, third = _record(), _record(), _record()
    launcher = _Listing((first, second, third))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(100, 30)) as pilot:
        await app.action_sessions()
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.highlighted = 2
        chosen = choices.get_option_at_index(2).id
        generation_before = app.screen._resting_generation
        objects_before = list(choices.options)
        prompt_before = str(choices.get_option_at_index(0).prompt)

        await pilot.resize_terminal(60, 30)
        await pilot.pause()

        assert app.screen._resting_generation == generation_before, (
            "the resize cleared and refilled the list, re-arming the deferred placement"
        )
        assert [id(option) for option in choices.options] == [
            id(option) for option in objects_before
        ], "the resize replaced the Option objects instead of re-laying them"
        assert choices.highlighted == 2
        assert choices.get_option_at_index(2).id == chosen
        assert str(choices.get_option_at_index(0).prompt) != prompt_before, (
            "the columns were never re-laid for the new width"
        )


async def test_a_resize_that_does_not_change_the_width_redraws_nothing() -> None:
    """Height alone is not a reason to touch the rows.

    Counted through `replace_option_prompt` rather than through the drawn output, because the
    re-lay at an unchanged width produces byte-identical prompts — so every assertion on what
    is *on screen* passes whether or not the work was done, and the thing being narrowed here
    is the work.

    `_resting_generation` is asserted beside it, and review is why. The old implementation
    reran `_draw_listing`, which never called `replace_option_prompt` either — so the counter
    alone reads zero against the old code as happily as against the new, and the half of this
    test that fails under mutation was only ever the width-change half. The generation counter
    is the instrument that can see the old body: a clear-and-refill takes a new generation, a
    skipped resize takes none.
    """
    first, second, third = _record(), _record(), _record()
    launcher = _Listing((first, second, third))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(100, 30)) as pilot:
        await app.action_sessions()
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)

        relays = 0
        original = choices.replace_option_prompt

        def counting_replace(*args: object, **kwargs: object) -> object:
            nonlocal relays
            relays += 1
            return original(*args, **kwargs)  # type: ignore[arg-type]

        choices.replace_option_prompt = counting_replace  # type: ignore[method-assign]

        generation_before = app.screen._resting_generation

        await pilot.resize_terminal(100, 20)
        await pilot.pause()
        assert relays == 0, "a height-only resize re-laid the columns for no reason"
        assert app.screen._resting_generation == generation_before, (
            "a height-only resize refilled the list, which is the shape this narrowed away"
        )

        await pilot.resize_terminal(70, 20)
        await pilot.pause()
        assert relays, "a width change did not re-lay the columns"


async def test_the_sessions_key_pressed_on_the_sessions_screen_keeps_the_cursor() -> None:
    """The sixth exit, and the one that proves the fifth was not the last.

    `Ctrl+S` is an app-level binding offered *on the sessions screen itself*, where it means
    "re-read this list" rather than "navigate to it" — `show_sessions` sees it is already the
    current screen and reloads in place. It reloaded with the default, so the cursor went to
    row 0, and `ctrl+s` then `s` issued a graceful stop against a session the owner never
    selected: the identical shape measured for `Ctrl+R`, one binding along.

    It survived Task 1.2's sweep because that sweep was `grep -nE 'self\\.reload\\(' on two
    screen modules, and this call is `screen.reload()` in `app.py` — the wrong spelling in the
    wrong file. The class was named correctly and enumerated too narrowly, which is the
    failure mode a sweep is supposed to prevent. The architecture test now parses every module
    under `adapters/tui`, so a seventh cannot hide in a third file either.
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

        await pilot.press("ctrl+s")
        await pilot.pause()

        after = app.screen.query_one("#choices", OptionList)
        assert after.highlighted is not None, "the re-read left the list with no cursor at all"
        resting_id = after.get_option_at_index(after.highlighted).id

    assert resting_id == chosen, "Ctrl+S re-chose the row instead of re-reading the list"


async def test_the_gauge_seed_stands_down_when_the_owner_has_left_and_returned() -> None:
    """The fourth unserialised fill, which carried neither of the two guards written for it.

    `_seed_context_gauges` reads records, awaits a per-session provider sweep, and then draws
    the records it read *before* that await. `_auto_reload` does the same shape and guards it
    twice — it sets `_reading` so a tick cannot run underneath, and it captures `_visit` across
    the await so a listing belonging to a visit the owner has left is dropped. The seed set
    neither, on the one path that runs at mount, where the provider sweep is slowest.

    The failure that leaves is the one `on_screen_resume` already documents in the other
    direction: "a session that ended during the detour is put back on screen by the stale
    listing". On a host where the sweep outruns the ten-second interval, the tick reads a list
    without the session that just ended, draws it, and correctly clears the cursor — and then
    the seed resumes and redraws its stale list with `keep_cursor=True`, putting the ended
    session back under the cursor that `s` acts on.

    Driven through `_visit` because that is the guard with a clean observable: bumping it is
    what "the owner left and came back" does, and a seed that ignores it draws onto a visit
    that is not its own.
    """
    first, second = _record(), _record()
    launcher = _Listing((first, second))
    app = RemoteAgentsTui(_context_with_usage(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SessionsScreen)

        drawn: list[int] = []
        original = screen._draw_listing

        def counting_draw(records, **kwargs):
            drawn.append(len(records))
            return original(records, **kwargs)

        screen._draw_listing = counting_draw  # type: ignore[method-assign]

        async def slow_refresh(records):
            # The owner leaves and comes back while the provider sweep is still running.
            screen._visit += 1

        app.refresh_context_windows = slow_refresh  # type: ignore[method-assign]
        await screen._seed_context_gauges()

        assert drawn == [], (
            "the gauge seed drew a listing belonging to a visit the owner had already left"
        )


async def test_the_gauge_seed_holds_the_reading_flag_across_its_await() -> None:
    """So a tick cannot run underneath it and be overwritten by its stale draw.

    The other half of `_auto_reload`'s pair. Without it the two fills interleave freely, and
    the one that lands last is whichever the scheduler happens to resume — which is the exact
    dependency the `_visit` counter was introduced to remove elsewhere in this file.
    """
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context_with_usage(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SessionsScreen)

        seen: list[bool] = []

        async def watching_refresh(records):
            seen.append(screen._reading)

        app.refresh_context_windows = watching_refresh  # type: ignore[method-assign]
        await screen._seed_context_gauges()

        assert seen == [True], f"the seed did not hold `_reading` across its await: {seen}"
        assert screen._reading is False, "the seed left `_reading` set"


async def test_the_gauge_seed_stands_down_when_any_fill_landed_during_its_await() -> None:
    """`_visit` is not enough, and the docstring that said it was is the reason this exists.

    The seed's first guard compared `_visit`, which is bumped only by `on_screen_resume` and
    `on_reveal` — navigation. `refresh_contents` (Ctrl+R) and `after_command` bump nothing, and
    `reload` deliberately does not stand down for `_reading`, because a keyed re-read is the
    owner asking again. So: the seed's provider sweep runs long, the owner presses Ctrl+R,
    `reload` reads fresh records and draws them, the seed resumes with `_visit` unchanged and
    redraws its stale list over the top. That is exactly the thing the seed's docstring claimed
    it could no longer do.

    The cursor survives it — both draws restore by key — but `_drawn` is rebound to the stale
    records, so `check_action` re-offers `s` and `c` on a row that has already gone until the
    next tick.

    Guarded on the fill counter instead, which every redraw takes whatever route it arrived by:
    `show_choices` bumps `_resting_generation` on every exit, so a seed that finds it moved
    knows a listing newer than its own is on screen and has nothing to add.
    """
    first, second = _record(), _record()
    launcher = _Listing((first, second))
    app = RemoteAgentsTui(_context_with_usage(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SessionsScreen)

        drawn: list[int] = []
        original = screen._draw_listing

        def counting_draw(records, **kwargs):
            drawn.append(len(records))
            return original(records, **kwargs)

        async def keyed_refresh_lands(records):
            # The owner presses Ctrl+R while the provider sweep is still running. No
            # navigation, so `_visit` does not move.
            screen._draw_listing = original  # type: ignore[method-assign]
            await screen.refresh_contents()
            screen._draw_listing = counting_draw  # type: ignore[method-assign]

        app.refresh_context_windows = keyed_refresh_lands  # type: ignore[method-assign]
        screen._draw_listing = counting_draw  # type: ignore[method-assign]
        visiting = screen._visit
        await screen._seed_context_gauges()

        assert screen._visit == visiting, "this test is only meaningful without a navigation"
        assert drawn == [], (
            "the gauge seed redrew its stale listing over a fresher one the owner asked for"
        )


async def test_a_trust_blocked_session_reads_untrusted_and_offers_no_answer_here() -> None:
    """DEC-047, drawn: the local surface shows the state and asks nothing.

    The console exchanges its left pane with the agent, so the dialog is already on screen in
    front of the owner. A Trust row here would be a second place to answer a question that is
    already answerable, and a Don't-trust row would be an unconfirmed kill one keypress from
    the resting cursor. The word is the whole of this surface's job.
    """
    launcher = _Listing((_record(SessionState.UNTRUSTED),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        rows = _rows(app)

    assert any("untrusted" in row for row in rows)
    assert not any("trust" in row.lower().replace("untrusted", "") for row in rows)


class _TrustBlockedLauncher(_Listing):
    """A launcher whose launch lands on the agent's folder-trust dialog."""

    launched: int = 0

    async def launch(self, _command):
        self.launched += 1
        return _record(SessionState.UNTRUSTED)


async def test_a_trust_blocked_launch_opens_the_session_rather_than_reporting_a_failure() -> None:
    """DEC-047's other half, at the moment it matters: the owner is handed the dialog.

    A trust-blocked launch used to land in FAILED, so this surface answered with the
    `LaunchFailure` branch — "The session did not become ready, but its pane may still
    exist", plus an attach command to copy. That is the wrong thing to say about an agent
    that is up and waiting for one keypress, and worse, it stops short of the one action that
    helps: exchanging the pane in so the owner can answer it.

    The state must therefore take the ordinary open route. The absence of a `LaunchFailure`
    is the assertion, and the console exchange it enables is what the whole no-trust-row
    argument on this surface rests on.
    """
    launcher = _TrustBlockedLauncher((_record(SessionState.UNTRUSTED),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        app.selection = dataclasses.replace(
            app.selection,
            project=_EXISTING,
            profile=ProfileAvailability("claude", True),
        )
        failure = await app.launch()
        await pilot.pause()

    # Absence proves nothing about a launch that never happened. `launch()` returns None
    # early when the wizard has gathered no project or profile, which is exactly the shape
    # that makes the assertion below pass for the wrong reason — and did, until a mutation
    # that should have reddened this test did not.
    assert launcher.launched == 1, "the launch never reached the backend; the assert is vacuous"
    assert failure is None, (
        "the local surface reported a trust-blocked launch as a failure. It is not one: the "
        "agent is up and asking a question, and this surface's job is to put its pane in "
        "front of the owner (DEC-047)"
    )
