"""The cursor and the acting target are two things on the sessions positions.

Before this, an arrow press *was* the choice: `s` ended whatever row the cursor had swept onto,
and the console's published selection was written from the same cursor, so every other pane
acted on it too. DEC-062 made that safe by taking the cursor away whenever the row it held left
the list — a mitigation for the hazard rather than a removal of it, and its own entry says so:
"one keypress, on a row the owner *is* looking at, irreversible and unasked".

The split removes the premise instead. The cursor is navigation and nothing else; the **active**
session is what the keys act on, and it changes on three deliberate acts — the owner committing
a row (enter, `d`, or `space`), the listing's first fill, and a session *starting*, which is the
owner naming it by starting it. So this module's tests are about which of the two moves when.

The rendering half is here too rather than in a snapshot, and for the reason DEC-010 gives about
every other signal on this surface: the marker is what tells the owner which agent an unconfirmed
`s` is pointed at, so it has to survive `NO_COLOR` as a character and not only as a colour.
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


def _marked(screen) -> list[str]:
    """The rows carrying the active marker. One at most, and none is a real answer."""
    return [row for row in _rendered(screen) if ACTIVE_MARKER in row]


async def test_a_first_fill_makes_the_top_row_the_target() -> None:
    """A list that opens with every key inert reads as a broken keymap, not as a safe one.

    Row 0 is the answer only on a *first* fill: there is nothing behind it the owner could have
    chosen instead, which is exactly what stops being true on every later draw.
    """
    app = SessionsPane(_context((_record(_FIRST, "one"), _record(_SECOND, "two", sequence=2))))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SessionsPaneScreen)
        assert screen.target_session() == str(_FIRST)
        assert screen.highlighted_session() == str(_FIRST), "the two agree on a first fill"


async def test_moving_the_cursor_does_not_move_the_target() -> None:
    """The whole of the split, in one arrow press.

    The cursor is where the owner is looking. `s` and `c` end a session without asking
    (DEC-018), and looking at a row is not a decision to end it.
    """
    console = SelectionConsole()
    app = SessionsPane(
        _context(
            (_record(_FIRST, "one"), _record(_SECOND, "two", sequence=2)),
            console_publish_selection=console.publish,
            console_read_selection=console.read,
        )
    )
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        await asyncio.sleep(0.05)
        published = len(console.published)

        screen.query_one("#choices", OptionList).focus()
        await pilot.press("down")
        await pilot.pause()
        await asyncio.sleep(0.05)

        assert screen.highlighted_session() == str(_SECOND), "the arrow moved no cursor"
        assert screen.target_session() == str(_FIRST), (
            "an arrow press retargeted an unconfirmed stop key"
        )
        assert len(console.published) == published, (
            "a cursor move wrote a selection every other pane would have acted on"
        )


async def test_space_commits_the_row_under_the_cursor() -> None:
    """The commit that is not also a navigation — see `_MAKE_ACTIVE_KEY`."""
    console = SelectionConsole()
    app = SessionsPane(
        _context(
            (_record(_FIRST, "one"), _record(_SECOND, "two", sequence=2)),
            console_publish_selection=console.publish,
            console_read_selection=console.read,
        )
    )
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#choices", OptionList).focus()
        await pilot.press("down")
        await pilot.press("space")
        await pilot.pause()
        await asyncio.sleep(0.05)

        assert screen.target_session() == str(_SECOND)
        assert console.published[-1] is not None
        assert str(console.published[-1]) == str(_SECOND), (
            "the panes with no sessions list act on the published value; it must be the target"
        )
        assert app.screen is screen, "`space` navigated somewhere, which is the one thing it is not"


async def test_enter_commits_the_session_it_exchanges_in() -> None:
    """Opening a row is naming it, so the keys follow the agent into the left slot.

    Without this the owner exchanges session B into the pane they are looking at and the bare
    `s` is still pointed at A — and the detail, which is `about_one_session`, would resolve
    differently from the list one escape away.
    """
    shown: list[str] = []

    async def show(session_id: str) -> None:
        shown.append(session_id)

    app = SessionsPane(
        _context(
            (_record(_FIRST, "one"), _record(_SECOND, "two", sequence=2)),
            open_in_console=show,
        )
    )
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#choices", OptionList).focus()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        assert shown == [str(_SECOND)]
        assert screen.target_session() == str(_SECOND)


async def test_a_session_that_starts_takes_the_cursor_and_the_target() -> None:
    """The owner named this session by starting it, so both marks land on it.

    Both, deliberately: the cursor so the row they were waiting for is the row they are looking
    at, and the target so the keys act on it without a second press. Two ways to get it wrong
    were available — moving neither (the owner hunts for the row they just created) and moving
    only one (the bold row and the marked row disagree, which is the state the split exists to
    make readable rather than to manufacture).
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
        assert screen.target_session() == str(_FIRST), "the first fill took row 0"

        launcher.records = (*launcher.records, _record(_THIRD, "fresh", sequence=3, age_seconds=0))
        await screen._auto_reload()
        await pilot.pause()
        await asyncio.sleep(0.05)

        assert screen.target_session() == str(_THIRD)
        assert screen.highlighted_session() == str(_THIRD)
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

        assert screen.target_session() == str(_SECOND)


async def test_a_first_fill_is_not_read_as_a_arrival() -> None:
    """Every row of a first fill is new and none of them arrived.

    Read as arrivals, the opening draw would put the target on whichever session happens to be
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
        assert screen.target_session() == str(_FIRST), "the youngest row was read as an arrival"


async def test_a_vanished_row_leaves_nothing_active() -> None:
    """`_CLEARS_VANISHED_ACTIVE`, which is DEC-062's mitigation moved to where the keys read.

    The flag in the module is what the import-time invariant reads; this is what makes it true.
    A session that ends between two ticks must leave the keys pointed at nothing, because the
    alternative — silently retargeting them at a neighbour on a timer nobody pressed — is the
    hazard DEC-052 named and DEC-062 traded the cursor away to close.
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
        assert screen.target_session() == str(_FIRST)

        launcher.records = (_record(_SECOND, "two", sequence=2),)
        await screen._auto_reload()
        await pilot.pause()
        await asyncio.sleep(0.05)

        assert screen.target_session() is None, "the keys were retargeted by a background tick"
        assert not _marked(screen), "a row is drawn as the target when nothing is"
        assert console.published[-1] is None, (
            "the other panes' chords were left pointed at a session that has ended"
        )


async def test_a_stop_that_raised_clears_the_target_it_failed_on() -> None:
    """`rest_on_nothing` outranks every other answer, and the row is why.

    The session the command failed on is demonstrably still live — the failure is why it is
    still there — and `s`/`c` carry no confirmation, so a repeated keypress must find nothing
    rather than re-issue a stop nobody chose (DEC-018, DEC-062).
    """
    console = SelectionConsole()
    app = SessionsPane(
        _context(
            (_record(_FIRST, "one"), _record(_SECOND, "two", sequence=2)),
            console_publish_selection=console.publish,
            console_read_selection=console.read,
        )
    )
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen.target_session() is not None

        await screen.redraw_after_failure()
        await pilot.pause()
        await asyncio.sleep(0.05)

        assert screen.target_session() is None
        assert not _marked(screen)
        assert console.published[-1] is None


async def test_a_failed_store_read_clears_the_target() -> None:
    """`draw_failure_rows` is the one fill where `_drawn` outlives the rows on screen.

    So the field is cleared there explicitly rather than left to `target_session`'s membership
    guard, which would answer from records that are no longer drawn.
    """
    console = SelectionConsole()
    app = SessionsPane(
        _context(
            (_record(_FIRST, "one"),),
            console_publish_selection=console.publish,
            console_read_selection=console.read,
        )
    )
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen.target_session() is not None

        screen.draw_failure_rows((("\x00back", "Back"),))
        await pilot.pause()
        await asyncio.sleep(0.05)

        assert screen.target_session() is None
        assert console.published[-1] is None


async def test_the_target_row_is_marked_by_a_character_and_not_only_by_colour() -> None:
    """DEC-010's rule, applied to the signal that says which agent `s` will end.

    Under `NO_COLOR` the yellow identity is byte-identical to every other row, so the marker is
    the signal and the colour is the second one. The column is reserved on every row of a
    listing that marks a target, so the rows do not shift by two cells when nothing is active.
    """
    app = SessionsPane(_context((_record(_FIRST, "one"), _record(_SECOND, "two", sequence=2))))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        rows = _rendered(screen)

        assert rows[0].startswith(ACTIVE_MARKER), "the target row carries no marker"
        assert not rows[1].startswith(ACTIVE_MARKER)
        assert len({len(row) for row in rows}) == 1, (
            "the unmarked rows did not reserve the marker column, so they are laid out short"
        )


async def test_committing_a_row_moves_the_marker_without_refilling_the_list() -> None:
    """The repaint is in place, which is what keeps `space` from disturbing the cursor.

    A clear-and-refill would bump `_resting_generation`, schedule a fresh `_rest_cursor`, and
    re-arm the window an arrow press can land in — on the one key whose whole purpose is to
    leave the cursor exactly where the owner put it.
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
        await pilot.press("space")
        await pilot.pause()

        assert _rendered(screen)[1].startswith(ACTIVE_MARKER), "the marker did not follow"
        assert not _rendered(screen)[0].startswith(ACTIVE_MARKER)
        assert screen._resting_generation == generation, "`space` refilled the list"
        assert [id(option) for option in choices.options] == [id(option) for option in options], (
            "the rows were replaced rather than repainted, which drops a queued selection"
        )


async def test_the_row_keys_act_on_the_target_rather_than_on_the_cursor() -> None:
    """The reason the split exists, driven through a real keypress rather than asserted.

    `i` opens the detail for the session it acts on, which is the cheapest of the row keys to
    observe and takes the same `target_session()` route as `s` and `c` do.
    """
    from remote_agents.adapters.tui.screens.sessions import SessionDetailScreen

    app = SessionsPane(_context((_record(_FIRST, "one"), _record(_SECOND, "two", sequence=2))))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#choices", OptionList).focus()
        await pilot.press("down")
        await pilot.press("i")
        await pilot.pause()

        assert isinstance(app.screen, SessionDetailScreen)
        assert app.screen.session_value == str(_FIRST), (
            "a row key acted on the row the cursor had swept onto rather than on the target"
        )
