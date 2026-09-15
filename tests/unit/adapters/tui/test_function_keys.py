"""The F-key row: one table, bound on the app, acting exactly as the Alt chords did.

Sub-plan 03 Task 1.1. Every check here reads `adapters/tui/keys.py::FUNCTION_KEYS` rather
than spelling the keys out, so a row added to the table is a row covered here on the same
commit -- and the session-shaped keys (F3, F4, F6, F8, F9) are driven through the same doubles
the chord layer's tests use, because the claim is that they resolve their session the same way.

Test names deliberately avoid the substrings later tasks reserve for their own `-k` selectors.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from backends import SessionUseCaseDouble, backend_for
from console_selection import SelectionConsole
from stop_results import a_clean_stop, a_stop_that_did_not_take
from textual._xterm_parser import XTermParser
from textual.binding import Binding
from textual.events import Key
from tui_feedback import announcements, status
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.keys import (
    FUNCTION_KEYS,
    SESSION_KEYS,
    FunctionKey,
    function_key_bindings,
)
from remote_agents.adapters.tui.panes import FeedPane, ProjectsPane, SessionsPane
from remote_agents.adapters.tui.screens.confirm import ForceConfirmModal
from remote_agents.adapters.tui.screens.sessions import (
    CHORD_KEYS,
    CHORD_STOPS,
    RenameScreen,
    SessionDetailScreen,
)
from remote_agents.application.commands import (
    CleanupCommand,
    ForceStopCommand,
    GracefulStopCommand,
)
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
from remote_agents.ports.terminal import TerminalObservation

_EXISTING = CatalogProject("opaque-existing", "existing", "infra", "Registered")

#: What an xterm-family terminal sends for each function key, by the name Textual gives it.
#: F11 is included on purpose: the terminal delivers it like any other, so its absence from
#: the table has to be the table's doing and not the terminal's.
_XTERM_SEQUENCES = {
    "f1": "\x1bOP",
    "f2": "\x1bOQ",
    "f3": "\x1bOR",
    "f4": "\x1bOS",
    "f5": "\x1b[15~",
    "f6": "\x1b[17~",
    "f7": "\x1b[18~",
    "f8": "\x1b[19~",
    "f9": "\x1b[20~",
    "f10": "\x1b[21~",
    "f11": "\x1b[23~",
    "f12": "\x1b[24~",
}


def _record(state: SessionState = SessionState.RUNNING, *, ordinal: int = 1) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", ordinal),
        state,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _Listing(SessionUseCaseDouble):
    records: tuple[SessionRecord, ...] = ()
    #: Every command issued, in order -- the id matters, not only the count, because a key
    #: acting on the pane's own rows instead of the published selection issues one too.
    issued: list[object] = field(default_factory=list)
    observation: TerminalObservation | None = None

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        return None

    async def graceful_stop(self, command: GracefulStopCommand) -> TerminalObservation:
        self.issued.append(command)
        return self.observation or a_clean_stop()

    async def cleanup(self, command: CleanupCommand) -> None:
        self.issued.append(command)

    async def force_stop(self, command: ForceStopCommand) -> TerminalObservation:
        self.issued.append(command)
        return a_clean_stop()


class _Creator:
    def available_areas(self) -> tuple[str, ...]:
        return ("infra",)


class _Capture:
    """Records which session's output was asked for, which is how F3 names its row."""

    def __init__(self) -> None:
        self.asked: list[SessionId] = []

    async def __call__(self, session_id: SessionId) -> str:
        self.asked.append(session_id)
        return "captured output"


def _context(launcher: _Listing, *, capture: _Capture | None = None) -> TuiContext:
    return TuiContext(
        backend=backend_for(
            sessions=launcher,  # type: ignore[arg-type]
            projects=_Creator(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_EXISTING,),
            catalogue=(_EXISTING,),
            capture=capture,
        ),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


def _on_a_console(launcher: _Listing, console: SelectionConsole) -> TuiContext:
    return replace(
        _context(launcher),
        console_read_selection=console.read,
        console_holds_slot=console.holds_console_slot,
    )


def _action_name(action: str) -> str:
    """`session_key('inspect')` -> `session_key`; `refresh` -> `refresh`."""
    return action.split("(", 1)[0]


# --- the table --------------------------------------------------------------------------


def test_the_table_is_eleven_keys_and_f11_is_not_one_of_them() -> None:
    """F1-F10 and F12, and F11 absent by construction rather than by omission."""
    assert isinstance(FUNCTION_KEYS, tuple), "the table must be a literal tuple, not derived"
    assert all(isinstance(entry, FunctionKey) for entry in FUNCTION_KEYS)
    keys = [entry.key for entry in FUNCTION_KEYS]
    assert len(keys) == 11, f"the table has {len(keys)} entries: {keys}"
    assert len(set(keys)) == 11, f"a key is bound twice: {keys}"
    assert "f11" not in keys, "F11 is reserved and must not be in the table"
    assert set(keys) == {f"f{n}" for n in range(1, 11)} | {"f12"}


def test_the_table_reads_in_key_order_with_a_label_and_a_borrowing_each() -> None:
    """Prose generators will read this table, so its shape is part of the contract."""
    numbers = [int(entry.key[1:]) for entry in FUNCTION_KEYS]
    assert numbers == sorted(numbers), "the table is not in F-key order"
    for entry in FUNCTION_KEYS:
        assert entry.label and entry.label == entry.label.lower(), (
            f"{entry.key}: the footer reads lower-case labels, got {entry.label!r}"
        )
        assert entry.borrowed_from, f"{entry.key} says nothing about where it was borrowed from"


def test_every_table_entry_names_an_action_the_app_has() -> None:
    """A bound key whose action does not exist is a key that raises out of a keypress."""
    missing = [
        entry.key
        for entry in FUNCTION_KEYS
        if not callable(getattr(RemoteAgentsTui, f"action_{_action_name(entry.action)}", None))
    ]
    assert not missing, f"these keys name actions the app does not have: {missing}"


def test_the_session_shaped_keys_name_a_row_key_the_chord_layer_carries() -> None:
    """F3/F4/F6/F8/F9 inherit the chords' bounds by naming the chords' own row letters.

    Reserved names, so the parametrised action is `session_key('<name>')` and nothing else:
    the five names are the vocabulary `action_session_key` accepts, and each resolves to a
    letter `_offers_chords` already rules on. F8 and F9 must land on the stop letters, since
    that is what gets them refused on a commitment screen (DEC-052/DEC-062 as carried forward).
    """
    by_name = {session_key.name: session_key for session_key in SESSION_KEYS}
    assert set(by_name) == {"inspect", "detail", "rename", "graceful", "force"}
    for session_key in SESSION_KEYS:
        assert session_key.row_key in CHORD_KEYS, (
            f"{session_key.name} names the row key {session_key.row_key!r}, "
            "which the chord layer does not carry"
        )
    assert {by_name["graceful"].row_key, by_name["force"].row_key} <= CHORD_STOPS
    assert by_name["detail"].action is None, "F4 opens the detail and performs no action"
    bound = {entry.action for entry in FUNCTION_KEYS if _action_name(entry.action) == "session_key"}
    assert bound == {f"session_key('{name}')" for name in by_name}, (
        f"the F-keys and the session-key vocabulary disagree: {bound}"
    )


def test_the_terminal_delivers_each_key_under_the_name_the_table_binds() -> None:
    """The xterm sequences arrive as `f1`..`f12`, which is what the bindings are keyed by.

    Driven through Textual's own parser on the pinned version, because a table keyed by names
    the terminal never sends would bind eleven keys that can never be pressed.
    """
    parser = XTermParser()
    delivered = {}
    for name, sequence in _XTERM_SEQUENCES.items():
        events = list(parser.feed(sequence))
        assert len(events) == 1 and isinstance(events[0], Key), f"{name}: {events}"
        delivered[name] = events[0].key
    assert delivered == {name: name for name in _XTERM_SEQUENCES}
    for entry in FUNCTION_KEYS:
        assert delivered[entry.key] == entry.key
    # The terminal does send F11; the table is what leaves it out.
    assert delivered["f11"] == "f11"


def test_every_binding_built_from_the_table_is_priority_and_shown() -> None:
    bindings = function_key_bindings()
    assert len(bindings) == len(FUNCTION_KEYS)
    for entry, binding in zip(FUNCTION_KEYS, bindings, strict=True):
        assert isinstance(binding, Binding)
        assert binding.key == entry.key
        assert binding.action == entry.action
        assert binding.description == entry.label
        assert binding.priority is True, f"{entry.key} is not a priority binding"
        assert binding.show is True, f"{entry.key} is hidden from the footer"


def test_the_app_binds_every_table_key_as_built() -> None:
    """`RemoteAgentsTui.BINDINGS` carries the table, and nothing else claims an F-key."""
    declared = {binding.key: binding for binding in RemoteAgentsTui.BINDINGS}
    for entry in FUNCTION_KEYS:
        assert entry.key in declared, f"{entry.key} is not bound on the app"
        assert declared[entry.key].action == entry.action
        assert declared[entry.key].priority is True
        assert declared[entry.key].show is True
    stray = sorted(
        key
        for key in declared
        if key.startswith("f") and key[1:].isdigit() and key not in {e.key for e in FUNCTION_KEYS}
    )
    assert not stray, f"F-keys bound outside the table: {stray}"


# --- the session-shaped keys, driven as keypresses ---------------------------------------


async def test_f3_on_the_sessions_pane_inspects_the_highlighted_row() -> None:
    """Inspect on the sessions pane resolves the row the pane has highlighted, like `i`."""
    chosen = _record()
    capture = _Capture()
    app = SessionsPane(_context(_Listing((chosen,)), capture=capture))

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert app.screen.highlighted_session() == str(chosen.session_id)

        await pilot.press("f3")
        await pilot.pause()

        reached = position(app)

    assert reached == "INSPECT", f"F3 reached {reached}"
    assert capture.asked == [chosen.session_id], "inspect captured the wrong session"


async def test_f4_from_a_pane_with_no_list_opens_the_published_selection_s_detail() -> None:
    """The layer's whole point, on the one key that names no action."""
    chosen = SessionId.new()
    console = SelectionConsole(selected=chosen)
    app = ProjectsPane(_on_a_console(_Listing(()), console))

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        reads, slot_reads = console.reads, console.slot_reads

        await pilot.press("f4")
        await pilot.pause()

        assert isinstance(app.screen, SessionDetailScreen)
        assert app.screen.session_value == str(chosen)
        # The gate and the selection are read per press, never cached (DEC-073(3)).
        assert console.reads - reads == 1 and console.slot_reads - slot_reads == 1

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SessionDetailScreen), "escape did not come home"


async def test_f8_from_the_feed_pane_stops_the_published_selection_and_says_how_it_went() -> None:
    """DEC-018: one graceful stop, no modal, against the published id, from a listless pane.

    Driven with a stop that did not take, so there is something to announce -- and asserted
    in the toast rather than the status line, which on the feed pane describes the feed.
    """
    chosen = _record()
    console = SelectionConsole(selected=chosen.session_id)
    listing = _Listing((chosen,), observation=a_stop_that_did_not_take("the pane is still alive"))
    app = FeedPane(_on_a_console(listing, console))

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        before = status(app)

        await pilot.press("f8")
        await pilot.pause()

        asked = isinstance(app.screen, ForceConfirmModal)
        after = status(app)
        said = announcements(app)
        if asked:
            # A modal left up at block exit hangs the teardown; dismiss it and let `asked`
            # report the regression as a failure instead.
            await pilot.press("escape")
            await pilot.pause()

    assert [type(command) for command in listing.issued] == [GracefulStopCommand]
    assert listing.issued[0].session_id == chosen.session_id
    assert not asked, "a graceful stop asked first"
    assert after == before, "the outcome was written over the feed's own status"
    assert any("did not" in words or "still alive" in words for words in said), (
        "the stop's outcome was not announced"
    )


async def test_f9_posts_the_force_question_and_it_rests_on_abort() -> None:
    """DEC-025/DEC-068: F9 posts, the screen's handler asks, and the modal defaults to abort.

    Captured inside the block, asserted outside: an assertion raised while the confirmation is
    up leaves its worker waiting on an answer and presents as a hung suite.
    """
    chosen = _record()
    console = SelectionConsole(selected=chosen.session_id)
    listing = _Listing((chosen,))
    app = FeedPane(_on_a_console(listing, console))

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        await pilot.press("f9")
        await pilot.pause()

        asked = isinstance(app.screen, ForceConfirmModal)
        choices = app.screen.query_one("#choices")
        resting = [option.id for option in choices.options][choices.highlighted]

        await pilot.press("escape")
        await pilot.pause()
        dismissed = not isinstance(app.screen, ForceConfirmModal)
        alive = app.is_running

    assert asked, "F9 did not ask before killing"
    assert resting != "force-confirm", "the destructive option must not be preselected"
    assert dismissed, "escape did not dismiss the modal"
    assert listing.issued == [], "an aborted confirmation issued a stop anyway"
    assert alive, "the surface stopped answering after the modal"


async def test_f6_renames_the_published_selection_through_the_row_path() -> None:
    chosen = _record()
    console = SelectionConsole(selected=chosen.session_id)
    opened: list[tuple[str, str | None]] = []
    app = ProjectsPane(_on_a_console(_Listing((chosen,)), console))

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        async def _record_detail(value: str, action: str | None = None) -> None:
            opened.append((value, action))

        app.show_detail = _record_detail  # type: ignore[assignment]
        await pilot.press("f6")
        await pilot.pause()

    assert opened == [(str(chosen.session_id), "rename")]


async def test_the_stop_keys_are_refused_on_the_rename_box_and_the_others_are_not() -> None:
    """F8/F9 inherit exactly `alt+s`/`alt+f`'s bounds on a screen that commits typed text.

    The binding is `priority=True`, so the `Input` never sees the key; the refusal has to be
    `check_action`'s, and it is asserted both as the answer and as the absence of a stop.
    """
    chosen = _record()
    console = SelectionConsole(selected=chosen.session_id)
    listing = _Listing((chosen,))
    app = ProjectsPane(_on_a_console(listing, console))

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await app.push_screen(RenameScreen(str(chosen.session_id)))
        await pilot.pause()
        assert position(app) == "RENAME"

        offered = {
            session_key.name: app.check_action("session_key", (session_key.name,))
            for session_key in SESSION_KEYS
        }

        await pilot.press("f8")
        await pilot.pause()
        await pilot.press("f9")
        await pilot.pause()

        landed = position(app)
        # Escape first, assert after: if the gate were gone, F9 would have posted the force
        # question and a modal left up at block exit hangs `run_test`'s teardown rather than
        # failing. Dismissing it turns that regression into a failure `landed` can report.
        if isinstance(app.screen, ForceConfirmModal):
            await pilot.press("escape")
            await pilot.pause()

    assert offered["graceful"] is False, "F8 is live on the rename box"
    assert offered["force"] is False, "F9 is live on the rename box"
    assert offered["inspect"] is True and offered["detail"] is True and offered["rename"] is True
    assert listing.issued == [], f"a refused stop key issued {listing.issued}"
    assert landed == "RENAME", f"a refused stop key moved the owner to {landed}"


async def test_a_session_key_with_nothing_selected_warns_and_goes_nowhere() -> None:
    """DEC-027: the key warns on itself; it never asks and never navigates as a refusal."""
    console = SelectionConsole(selected=None)
    app = ProjectsPane(_on_a_console(_Listing(()), console))

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        before = position(app)

        await pilot.press("f4")
        await pilot.pause()

        assert position(app) == before
        assert announcements(app, severity="warning") == ["No session is selected."]


async def test_no_session_key_fires_behind_a_modal() -> None:
    """The other side of DEC-025's hazard: nothing pops a modal out from under its handler."""
    console = SelectionConsole(selected=SessionId.new())
    app = ProjectsPane(_on_a_console(_Listing(()), console))

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.push_screen(ForceConfirmModal("Force stop this session?"))
        await pilot.pause()

        refused = {
            session_key.name: app.check_action("session_key", (session_key.name,))
            for session_key in SESSION_KEYS
        }
        assert app.check_action("projects_home", ()) is False
        reads = console.reads

        await pilot.press("f4")
        await pilot.press("f12")
        await pilot.pause()

        assert isinstance(app.screen, ForceConfirmModal), "a key navigated out from under a modal"
        assert console.reads == reads, "a key behind a modal read the console's selection"

    assert all(answer is False for answer in refused.values()), refused


# --- F12 ----------------------------------------------------------------------------------


async def test_f12_unwinds_to_the_projects_position() -> None:
    chosen = SessionId.new()
    console = SelectionConsole(selected=chosen)
    app = ProjectsPane(_on_a_console(_Listing(()), console))

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        home = position(app)
        await app.push_screen(SessionDetailScreen(str(chosen)))
        await pilot.pause()
        assert position(app) != home

        await pilot.press("f12")
        await pilot.pause()

        assert position(app) == home, f"F12 landed on {position(app)}"
        assert len(app.screen_stack) == 1
