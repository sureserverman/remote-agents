"""The row a key will act on is drawn as such, and a session that starts becomes it.

Two things that used to be invisible or absent, and neither of them changes *which* session the
keys act on: that is the row under the cursor, as it has always been.

**The marker.** `s` and `c` end a session without asking (DEC-018), and the pane that carries
those keys is one of four in the console — so from the projects or feed pane, `⌥s` acts on a row
in a list you are not focused on and whose cursor you were not watching. The `▸` and the yellow
identity are how that row says so. It is a *rendering of the cursor*, never a second selection;
`test_the_marker_is_the_cursor_and_never_a_second_selection` is what holds that, because two
answers to "which session" is the design that was built here first and reverted.

**The arrival.** A session that has just started takes the cursor, so the agent you launched is
the one in front of you and the one your keys are pointed at without a hunt down the list.

The safety property is unchanged and is asserted here too: a row that leaves the listing leaves
the cursor — and so the marker, and so the published selection — on nothing at all.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from backends import SessionUseCaseDouble, tui_context_for
from console_selection import SelectionConsole
from textual.widgets import OptionList

from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.panes import SessionsPane
from remote_agents.adapters.tui.rows import ACTIVE_MARKER
from remote_agents.adapters.tui.screens.sessions import SessionsPaneScreen
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

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_FIRST = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_SECOND = SessionId.parse("fedcba98-7654-3210-fedc-ba9876543210")
_THIRD = SessionId.parse("11111111-2222-3333-4444-555555555555")


class _Launcher(SessionUseCaseDouble):
    def __init__(self, records: tuple[SessionRecord, ...]) -> None:
        self.records = records

    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records


def _record(
    session_id: SessionId, name: str, *, sequence: int = 1, age_seconds: int = 600
) -> SessionRecord:
    """One RUNNING session, `age_seconds` old.

    The age is a parameter because `_newest_arrival` breaks a tie by `created_at` rather than by
    list position — the store's order is not the clock's, and more than one session can appear
    between two ten-second ticks.
    """
    return SessionRecord(
        session_id,
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity(name, "claude", "regular", sequence),
        SessionState.RUNNING,
        datetime.now(UTC) - timedelta(seconds=age_seconds),
    )


def _context(records: tuple[SessionRecord, ...], **overrides) -> TuiContext:
    base = {
        "sessions": _Launcher(records),
        "projects": object(),
        "profiles": (ProfileAvailability("claude", True),),
        "refresh_catalogue": lambda: (_PROJECT,),
        "attach_argv": lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        "catalogue": (_PROJECT,),
        "capture": lambda _session_id: "captured output",
    }
    base.update(overrides)
    return tui_context_for(**base)


def _rendered(screen) -> list[str]:
    """Every drawn row as plain text, in order."""
    choices = screen.query_one("#choices", OptionList)
    return [str(choices.get_option_at_index(index).prompt) for index in range(choices.option_count)]


def _marked(screen) -> list[int]:
    """The indices of the rows carrying the marker. One at most, and none is a real answer."""
    return [index for index, row in enumerate(_rendered(screen)) if row.startswith(ACTIVE_MARKER)]


async def test_the_marker_is_the_cursor_and_never_a_second_selection() -> None:
    """The invariant the whole design rests on, asserted over every cursor move.

    A version of this pane was built where the marker was a *committed* target the cursor could
    wander away from, and it was reverted: the first question it produced was "if I press `s`,
    which one goes?", which is the one question a list carrying an unconfirmed stop key must
    never raise. One selection, drawn twice — bold for where you are, `▸` for what acts.

    Driven over the arrow keys rather than asserted once, because the marker is repainted from a
    highlight handler and the failure this guards is it lagging by one press.
    """
    records = (
        _record(_FIRST, "one"),
        _record(_SECOND, "two", sequence=2),
        _record(_THIRD, "three", sequence=3),
    )
    app = SessionsPane(_context(records))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SessionsPaneScreen)
        choices = screen.query_one("#choices", OptionList)
        choices.focus()

        for key in ("down", "down", "up", "end", "home", "down"):
            await pilot.press(key)
            await pilot.pause()
            assert _marked(screen) == [choices.highlighted], (
                f"after {key!r} the marker is on {_marked(screen)} and the cursor on "
                f"{choices.highlighted} — two answers to which session a key acts on"
            )


async def test_the_marker_names_the_row_the_stop_keys_act_on() -> None:
    """The marker's whole purpose, driven through a real key rather than asserted about state.

    `i` opens the detail for the session it acts on, which is the cheapest of the row keys to
    observe and takes the same `highlighted_session()` route `s` and `c` do.
    """
    from remote_agents.adapters.tui.screens.sessions import SessionDetailScreen

    app = SessionsPane(_context((_record(_FIRST, "one"), _record(_SECOND, "two", sequence=2))))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#choices", OptionList).focus()
        await pilot.press("down")
        await pilot.pause()
        marked = _marked(screen)

        await pilot.press("i")
        await pilot.pause()

        assert isinstance(app.screen, SessionDetailScreen)
        assert marked == [1], "this test needs the marker off row 0 to mean anything"
        assert app.screen.session_value == str(_SECOND), (
            "the key acted on a session other than the one the list marked"
        )


async def test_the_marker_is_a_character_and_not_only_a_colour() -> None:
    """DEC-010's rule, applied to the signal that says which agent `s` will end.

    Under `NO_COLOR` the yellow identity is byte-identical to every other row, so the marker is
    the signal and the colour is the second one. The column is reserved on every row, so the
    rows do not shift by two cells as the cursor moves or when nothing is marked at all.
    """
    app = SessionsPane(_context((_record(_FIRST, "one"), _record(_SECOND, "two", sequence=2))))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        rows = _rendered(app.screen)

        assert rows[0].startswith(ACTIVE_MARKER), "the cursor's row carries no marker"
        assert not rows[1].startswith(ACTIVE_MARKER)
        assert len({len(row) for row in rows}) == 1, (
            "the unmarked row did not reserve the marker column, so it is laid out short"
        )


async def test_moving_the_cursor_repaints_rather_than_refilling() -> None:
    """The marker moves in place, which is what keeps an arrow press from disturbing the list.

    A clear-and-refill would bump `_resting_generation`, schedule a fresh `_rest_cursor`, and
    re-arm the window an arrow press can land in — from a handler that runs *on* an arrow press,
    which is as close to that hazard as code can get.
    """
    app = SessionsPane(_context((_record(_FIRST, "one"), _record(_SECOND, "two", sequence=2))))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        choices = screen.query_one("#choices", OptionList)
        choices.focus()
        options = list(choices.options)
        generation = screen._resting_generation

        await pilot.press("down")
        await pilot.pause()

        assert _marked(screen) == [1], "the marker did not follow the cursor"
        assert screen._resting_generation == generation, "an arrow press refilled the list"
        assert [id(option) for option in choices.options] == [id(option) for option in options], (
            "the rows were replaced rather than repainted, which drops a queued selection"
        )


async def test_a_fill_does_not_repaint_the_rows_it_has_just_drawn() -> None:
    """`show_choices` assigns `highlighted`, which posts the same message an arrow does.

    Without the paint memo every draw would be followed by a redundant re-render of every row it
    had just rendered — on a ten-second timer, forever. Asserted by the memo agreeing with the
    drawn marker after a fill, which is the state that makes the handler stand down.
    """
    launcher = _Launcher((_record(_FIRST, "one"), _record(_SECOND, "two", sequence=2)))
    app = SessionsPane(_context((), sessions=launcher))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen._marked_row == str(_FIRST)

        await screen._auto_reload()
        await pilot.pause()

        assert screen._marked_row == str(_FIRST), "a tick moved the marker with no cursor move"
        assert _marked(screen) == [0]


async def test_a_session_that_starts_takes_the_cursor() -> None:
    """The owner named this session by starting it, so it is the one in front of them.

    The cursor, and therefore the marker, and therefore what a bare `s` acts on and what every
    other pane's chord resolves to — one move, because they are one thing.

    **The residual is named rather than reasoned away.** This listing's second writer is the
    bot, so a session started from Telegram moves this cursor too, and `s` carries no
    confirmation (DEC-018). That is the same single owner by construction, and the marked row
    says on screen which session moved.
    """
    launcher = _Launcher((_record(_FIRST, "one"), _record(_SECOND, "two", sequence=2)))
    console = SelectionConsole()
    app = SessionsPane(
        _context(
            (),
            sessions=launcher,
            console_publish_selection=console.publish,
            console_read_selection=console.read,
        )
    )
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen.highlighted_session() == str(_FIRST), "the first fill took row 0"

        launcher.records = (*launcher.records, _record(_THIRD, "fresh", sequence=3, age_seconds=0))
        await screen._auto_reload()
        await pilot.pause()
        await asyncio.sleep(0.05)

        assert screen.highlighted_session() == str(_THIRD)
        assert _marked(screen) == [2], "the new session is listed but not marked"
        assert str(console.published[-1]) == str(_THIRD), (
            "the new session is not what the other panes' chords would have acted on"
        )


async def test_the_youngest_arrival_wins_when_several_land_at_once() -> None:
    """More than one session can appear between two ten-second ticks.

    By `created_at` rather than by position: the listing's order is the store's, and the row the
    owner's last act produced is the youngest one rather than the first one drawn.
    """
    launcher = _Launcher((_record(_FIRST, "one"),))
    app = SessionsPane(_context((), sessions=launcher))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen

        launcher.records = (
            _record(_FIRST, "one"),
            _record(_THIRD, "older-arrival", sequence=3, age_seconds=30),
            _record(_SECOND, "younger-arrival", sequence=2, age_seconds=1),
        )
        await screen._auto_reload()
        await pilot.pause()

        assert screen.highlighted_session() == str(_SECOND)


async def test_a_first_fill_is_not_read_as_an_arrival() -> None:
    """Every row of a first fill is new and none of them arrived.

    Read as arrivals, the opening draw would rest the cursor on whichever session happens to be
    youngest on the host — a row nobody chose, on the one draw where row 0 is the argued answer.
    """
    app = SessionsPane(
        _context(
            (
                _record(_FIRST, "one", age_seconds=900),
                _record(_SECOND, "two", sequence=2, age_seconds=1),
            )
        )
    )
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen.highlighted_session() == str(_FIRST), (
            "the youngest row was read as an arrival"
        )


async def test_an_arrival_does_not_outrank_a_stop_that_raised() -> None:
    """`rest_on_nothing` is answer 1 for a reason, and an arrival must not displace it.

    The session a command failed on is demonstrably still live — the failure is why it is still
    there — and `s`/`c` carry no confirmation, so a repeated keypress must find nothing. A
    session arriving in the same redraw is not a reason to hand the keys a fresh subject.
    """
    launcher = _Launcher((_record(_FIRST, "one"),))
    app = SessionsPane(_context((), sessions=launcher))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen

        launcher.records = (
            _record(_FIRST, "one"),
            _record(_SECOND, "fresh", sequence=2, age_seconds=0),
        )
        await screen.redraw_after_failure()
        await pilot.pause()

        assert screen.highlighted_session() is None, "an arrival re-armed the keys after a failure"
        assert not _marked(screen)


async def test_a_vanished_row_leaves_nothing_marked() -> None:
    """`_CLEARS_VANISHED_CURSOR`, and the marker agreeing with it.

    The rule is unchanged and is what the import-time invariant reads; what this adds is that
    the *drawn* state says so too. A list with no cursor and no mark is the honest picture of a
    list whose keys will do nothing.
    """
    launcher = _Launcher((_record(_FIRST, "one"), _record(_SECOND, "two", sequence=2)))
    console = SelectionConsole()
    app = SessionsPane(
        _context(
            (),
            sessions=launcher,
            console_publish_selection=console.publish,
            console_read_selection=console.read,
        )
    )
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#choices", OptionList).highlighted = 1
        await pilot.pause()
        await asyncio.sleep(0.05)
        assert _marked(screen) == [1]

        launcher.records = (_record(_FIRST, "one"),)
        await screen._auto_reload()
        await pilot.pause()
        await asyncio.sleep(0.05)

        assert screen.highlighted_session() is None, "the keys were retargeted by a background tick"
        assert not _marked(screen), "a row is drawn as acted-on when no row is"
        assert console.published[-1] is None, (
            "the other panes' chords were left pointed at a session that has ended"
        )
