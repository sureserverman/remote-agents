"""Ctrl+S reaches the sessions view from anywhere the wizard can be, and nowhere unsafe."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest
from backends import SessionUseCaseDouble, backend_for
from console_selection import SelectionConsole
from stop_results import a_clean_stop, a_stop_that_did_not_take
from textual.widgets import Input, OptionList
from tui_feedback import announcements, status
from tui_filter import settle_filter
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.panes import FeedPane, LimitsPane, ProjectsPane, SessionsPane
from remote_agents.adapters.tui.screens.confirm import ForceConfirmModal
from remote_agents.adapters.tui.screens.sessions import (
    InspectScreen,
    SessionDetailScreen,
    SessionsScreen,
)
from remote_agents.application.commands import (
    CleanupCommand,
    ForceStopCommand,
    GracefulStopCommand,
)
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_views import with_project_names
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


def _record(state: SessionState = SessionState.RUNNING, *, ordinal: int = 1) -> SessionRecord:
    """One session record. `ordinal` exists so two records can be told apart *on screen*.

    Without it every `_record()` renders the identical display name, so an assertion of the form
    "the modal names A and not B" is unfalsifiable — it fails whichever session the code picked.
    The *project* is not a usable discriminator here even though the record carries one:
    `with_project_names` resolves every slug to the catalogue's single name before rendering, so
    only the sequence survives to the screen.
    """
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
    refreshed: int = 0
    #: Every command the surface issued, in order. A list rather than a count, because the
    #: chord tests need to say *which session* was stopped as well as how many times: a chord
    #: acting on the pane's own rows instead of the published selection would issue exactly one
    #: command too, and only the id tells the two apart.
    issued: list[object] = field(default_factory=list)
    #: Raised by every stop, so the failure path can be driven. The success path is the default
    #: because most cases want it — which is exactly why the failure path went unexamined until
    #: a review asked what a failed chord stop does to a pane that shows no sessions.
    error: Exception | None = None
    #: What `graceful_stop` reports back. `None` means the clean exit most cases assume; a
    #: not-taken observation is how the "dispatched but did not work" branch is driven, which is
    #: an ordinary outcome rather than an error path and has its own on-screen reporting.
    observation: TerminalObservation | None = None
    #: Raised by the listing reads, so `report_store_failure` can be driven. Separate from
    #: `error`, which is about the *stop* failing: an unreadable store and a stop that raised
    #: reach different reporting paths and only one of them redraws.
    read_error: Exception | None = None

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        self.refreshed += 1
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        if self.read_error is not None:
            raise self.read_error
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        return None

    async def graceful_stop(self, command: GracefulStopCommand) -> TerminalObservation:
        self.issued.append(command)
        if self.error is not None:
            raise self.error
        return self.observation or a_clean_stop()

    async def cleanup(self, command: CleanupCommand) -> None:
        self.issued.append(command)
        if self.error is not None:
            raise self.error

    async def force_stop(self, command: ForceStopCommand) -> TerminalObservation:
        self.issued.append(command)
        if self.error is not None:
            raise self.error
        return a_clean_stop()


class _Creator:
    def available_areas(self) -> tuple[str, ...]:
        return ("infra",)


def _context(launcher: _Listing) -> TuiContext:
    return TuiContext(
        backend=backend_for(
            sessions=launcher,  # type: ignore[arg-type]
            projects=_Creator(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_EXISTING,),
            catalogue=(_EXISTING,),
        ),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


def test_ctrl_s_is_bound_and_shown_in_the_footer() -> None:
    bindings = {binding.key: binding for binding in RemoteAgentsTui.BINDINGS}
    assert "ctrl+s" in bindings
    assert bindings["ctrl+s"].action == "sessions"
    assert bindings["ctrl+s"].description


def test_the_existing_bindings_keep_their_behavior() -> None:
    """Adding a binding must not renumber or rebind what the owner already knows."""
    bindings = {binding.key: binding.action for binding in RemoteAgentsTui.BINDINGS}
    assert bindings["escape"] == "back"
    assert bindings["ctrl+r"] == "refresh"
    assert bindings["ctrl+n"] == "add_project"
    assert bindings["ctrl+q"] == "quit"


@pytest.mark.parametrize(
    "step_setup",
    ["projects", "chooser", "profiles", "areas"],
)
async def test_ctrl_s_opens_sessions_from_any_wizard_step(step_setup: str) -> None:
    """Ctrl+S reaches the sessions list from every position the launch wizard has.

    **Two of these cases were not reaching the positions they were named for, and the test was
    green throughout.** `"profiles"` walked one `choose` and stopped — which reached the agent
    list when it was written, and stopped doing so the day the Launch-or-Resume chooser was
    inserted between them (DEC-033). `"review"` walked a second `choose("claude")` on top,
    which is not a row the chooser offers, so it did nothing at all: both cases sat on
    `PROJECT_CHOOSER`, testing it twice under two wrong names while the agent list — the
    position with the most bindings of the three — went untested.

    Neither was caused by removing the review position; the collapse predates it, and removing
    the review is what made it visible, since `"review"` no longer names anything. Fixed by
    walking each case to the position it claims **and asserting it arrived**: a parametrization
    whose cases silently converge is one that reports four times the coverage it has, and the
    assertion is the only part of this that a future insertion cannot quietly undo.
    """
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))
    expected = {
        "projects": "DASHBOARD",
        "chooser": "PROJECT_CHOOSER",
        "profiles": "PROFILES",
        "areas": "AREAS",
    }[step_setup]

    async with app.run_test() as pilot:
        if step_setup in {"chooser", "profiles"}:
            await app.screen.choose("opaque-existing")
            await pilot.pause()
        if step_setup == "profiles":
            await app.screen.choose("launch")
            await pilot.pause()
        elif step_setup == "areas":
            await app.show_areas()
        await pilot.pause()
        assert position(app) == expected, (
            f"the {step_setup!r} setup reached {position(app)}, not {expected} — this case is "
            f"not testing the step it is named for"
        )

        await app.action_sessions()
        await pilot.pause()
        step = position(app)

    assert step == "SESSIONS"


async def test_ctrl_s_is_refused_while_busy() -> None:
    """Matching the existing guard on refresh and add-project."""
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await pilot.pause()
        # The dashboard's own mount reload is the baseline; the refusal is about the key
        # adding nothing on top of it.
        before = launcher.refreshed
        app._busy = True
        await app.action_sessions()
        await pilot.pause()
        step = position(app)

    assert step == "DASHBOARD"
    assert launcher.refreshed == before


async def test_pressing_the_key_actually_reaches_the_action() -> None:
    """Binding tables can be right while the keystroke still goes nowhere."""
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await pilot.pause()
        before = launcher.refreshed
        await pilot.press("ctrl+s")
        await pilot.pause()
        step = position(app)

    assert step == "SESSIONS"
    assert launcher.refreshed == before + 1


async def test_ctrl_r_on_the_sessions_list_re_lists_it_and_stays_put() -> None:
    """The one view whose answer goes stale on its own is the one Refresh used to abandon.

    A second process writes the same store, so the sessions list is the position where the
    owner has an actual reason to press Refresh. It re-read the *catalogue* and unwound to
    the project picker instead, which is neither of the two things the key promises here.
    """
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert position(app) == "SESSIONS"
        listed, depth = launcher.refreshed, len(app.screen_stack)

        await pilot.press("ctrl+r")
        await pilot.pause()

        assert launcher.refreshed == listed + 1, "refresh did not re-run the sessions load"
        assert position(app) == "SESSIONS"
        assert len(app.screen_stack) == depth, "refresh moved the owner off the sessions list"


async def test_ctrl_r_where_there_is_nothing_to_re_read_does_not_navigate() -> None:
    """A screen with nothing to refresh stays where it is, rather than unwinding the stack.

    Task 1.2 turns this into a disabled binding the footer stops advertising; until then the
    property that matters is that the key cannot move the owner.
    """
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("launch")
        await pilot.pause()
        assert position(app) == "PROFILES"
        depth = len(app.screen_stack)

        await pilot.press("ctrl+r")
        await pilot.pause()

        assert position(app) == "PROFILES"
        assert len(app.screen_stack) == depth


def test_no_app_binding_is_swallowed_by_a_focusable_widget() -> None:
    """A binding a focused widget also claims never reaches the app.

    Ctrl+E shipped briefly as the Resume key. Textual's Input binds it to `end`, and the
    app starts with the filter focused, so pressing it on the opening screen did nothing —
    invisible to any test that called the action method directly.
    """
    from textual.widgets import Input, OptionList

    from remote_agents.adapters.tui.app import RemoteAgentsTui

    def keys_of(source) -> dict[str, str]:
        found: dict[str, str] = {}
        for binding in source.BINDINGS:
            key = getattr(binding, "key", str(binding))
            for part in str(key).split(","):
                found[part.strip()] = getattr(binding, "action", "?")
        return found

    app_keys = keys_of(RemoteAgentsTui)
    for widget in (Input, OptionList):
        clashes = {
            key: (app_keys[key], keys_of(widget)[key]) for key in app_keys if key in keys_of(widget)
        }
        assert not clashes, f"{widget.__name__} swallows {clashes}"


def test_every_screen_that_advertises_refresh_actually_implements_it() -> None:
    """`can_refresh` and `refresh_contents` are two declarations of one fact.

    The flag exists because the next task drives `check_action` off it, and asking "did this
    class replace a method" is not a question a binding check should be answering at runtime.
    That is a fair call, and it leaves the two free to disagree — with the footer taking the
    flag's word for it.

    Both directions are defects, and they are the *same* defect this task exists to fix, moved
    up a level: a screen that advertises Refresh without implementing it lies to the owner
    exactly as the old unconditional catalogue re-read did, and one that implements it without
    advertising it has working behaviour the next task will make unreachable.
    """
    from remote_agents.adapters.tui.screens import ALL_SCREENS
    from remote_agents.adapters.tui.screens.base import ChoiceScreen

    def implements(screen) -> bool:
        return screen.refresh_contents is not ChoiceScreen.refresh_contents

    disagreeing = {
        screen.__name__: (screen.can_refresh, implements(screen))
        for screen in ALL_SCREENS
        if issubclass(screen, ChoiceScreen) and screen.can_refresh != implements(screen)
    }
    assert not disagreeing, (
        "these screens declare `can_refresh` and override `refresh_contents` inconsistently "
        f"— (can_refresh, overrides) per screen: {disagreeing}"
    )


async def test_the_selection_capability_is_absent_off_a_console() -> None:
    """Declared absence, not a probe (DEC-046).

    A surface that is not hosted by a console has no published selection to read and nothing
    to publish to. Both capabilities are then `None`, and the Alt chord layer is not offered at
    all rather than offered and inert — a dead-end key is worse than an absent one, which is
    the same reasoning that gates `p` to the sessions pane.
    """
    context = _context(_Listing(()))

    assert context.console_publish_selection is None
    assert context.console_read_selection is None


async def test_the_selection_capability_publishes_and_reads_when_wired() -> None:
    """Two fields rather than one object, because no pane needs both.

    Publishing belongs to the one position that owns a cursor; reading belongs to every
    position that does not. Splitting them means a pane holding only the reader cannot
    accidentally become a second writer of a fact that must have exactly one.
    """
    console = SelectionConsole()
    chosen = SessionId.new()
    context = replace(
        _context(_Listing(())),
        console_publish_selection=console.publish,
        console_read_selection=console.read,
    )

    await context.console_publish_selection(chosen)
    await context.console_publish_selection(None)
    console.selected = chosen

    assert console.published == [chosen, None]
    assert await context.console_read_selection() == chosen


async def test_selected_session_on_a_sessions_position_answers_from_its_own_cursor() -> None:
    """A position with a cursor never asks the console what it already knows.

    Reading the published option here would answer with whatever the *pane* has highlighted,
    which on the full sessions position is a different process's cursor entirely — and on the
    pane itself would be a round trip to read back what it had just written.
    """
    console = SelectionConsole(selected=SessionId.new())
    records = (_record(), _record())
    app = RemoteAgentsTui(
        replace(
            _context(_Listing(records)),
            console_publish_selection=console.publish,
            console_read_selection=console.read,
        )
    )

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.highlighted = 1

        assert await app.selected_session() == choices.get_option_at_index(1).id
        assert console.reads == 0, "a position with its own cursor read the console anyway"


async def test_selected_session_elsewhere_answers_from_the_published_selection() -> None:
    """The projects pane has no sessions list, so the answer comes from the pane that does.

    `console_holds_slot` is wired here because this pane *is* one of the console's own — Stage
    3's read-gate asks that before the selection, and a pane that cannot answer it is refused.
    The refusal itself is asserted next door; this case is the ordinary one.
    """
    chosen = SessionId.new()
    console = SelectionConsole(selected=chosen)
    app = ProjectsPane(
        replace(
            _context(_Listing(())),
            console_publish_selection=console.publish,
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert not getattr(app.screen, "owns_session_cursor", False)

        # **Deltas, not totals.** Since Task 3.3 the pane also polls the selection on its own
        # timer to keep the hint row's dim/lit state current, so an absolute count here would
        # measure that poll as well and would drift with its cadence. What these assertions are
        # about is what *this resolver* did, which is the delta across the call.
        before = console.reads
        assert await app.selected_session() == str(chosen)
        assert console.reads - before == 1

        # Never cached: the owner can move the cursor in the other pane between two presses,
        # and the second press must act on where it is now.
        moved = SessionId.new()
        console.selected = moved
        before = console.reads
        assert await app.selected_session() == str(moved)
        assert console.reads - before == 1, "the second press reused a cached selection"


async def test_selected_session_is_nothing_off_a_console() -> None:
    """No cursor here and no console to ask, so there is nothing for a chord to act on.

    The chord layer is not offered at all in this case (Task 3.1), but this is the value it is
    gated on, and "nothing" has to be a returned answer rather than a raised one.
    """
    app = ProjectsPane(_context(_Listing(())))

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert app.services.console_read_selection is None
        assert await app.selected_session() is None


async def test_a_console_that_cannot_be_asked_selects_nothing() -> None:
    """A read that raises must not take the app down with it.

    `read_selection` shells out to tmux and a server that has gone away exits non-zero, which
    `AsyncTmuxRunner` raises. This resolver runs from a keypress, and every sibling in this
    package catches for exactly that reason — an exception out of a key handler exits the app.
    This one was the exception, and it is the one a destructive chord will call on every press.
    """

    async def unreachable() -> SessionId | None:
        raise RuntimeError("no server on that socket")

    app = ProjectsPane(replace(_context(_Listing(())), console_read_selection=unreachable))

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        assert await app.selected_session() is None
        assert app.is_running, "a failed selection read ended the surface"


async def test_an_alt_chord_acts_on_the_published_selection_and_leaves_the_filter_alone() -> None:
    """The owner's ask, in one test: bare letters are text, Alt letters are session actions.

    `priority=True` is what makes both halves true at once. Textual checks priority bindings
    from the App down *before* the focused widget (`App._check_bindings`), so `alt+i` never
    reaches the filter's `Input`, and `i` never reaches the chord layer. Measured on the
    pinned Textual 8.2.8 through a real tmux `send-keys M-i` before the layer was written.

    The read is taken per press and the gate with it — neither is cached, because the owner can
    move the cursor in the sessions pane, and an exchange can move *this* pane, between two
    presses.
    """
    chosen = SessionId.new()
    console = SelectionConsole(selected=chosen)
    opened: list[tuple[str, str | None]] = []
    app = ProjectsPane(
        replace(
            _context(_Listing(())),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        async def _record_detail(value: str, action: str | None = None) -> None:
            opened.append((value, action))

        app.show_detail = _record_detail  # type: ignore[assignment]
        await pilot.press("/")
        await pilot.press(*"ab")
        await settle_filter(pilot)
        entry = app.screen.query_one("#filter", Input)
        assert entry.has_focus and entry.value == "ab", "the filter never took the typed text"

        reads, slot_reads = console.reads, console.slot_reads
        await pilot.press("alt+i")
        await pilot.pause()

        assert opened == [(str(chosen), "inspect")]
        assert entry.value == "ab", "the chord's letter was typed into the filter as well"
        # Deltas: the hint row's own poll reads the same double (see the note in
        # `test_selected_session_elsewhere_answers_from_the_published_selection`).
        assert console.reads - reads == 1 and console.slot_reads - slot_reads == 1


async def test_an_alt_chord_really_navigates_and_does_not_only_resolve() -> None:
    """The end-to-end half of the test above, which stubs `show_detail` to read its arguments.

    `alt+d` is the one chord that names no action, so it is the one that can be driven all the
    way to a screen without a backend having to answer for an action on the far side.
    """
    chosen = SessionId.new()
    console = SelectionConsole(selected=chosen)
    app = ProjectsPane(
        replace(
            _context(_Listing(())),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        await pilot.press("alt+d")
        await pilot.pause()

        assert isinstance(app.screen, SessionDetailScreen)
        assert app.screen.session_value == str(chosen)


async def test_an_alt_chord_is_not_offered_off_a_console() -> None:
    """A dead-end key is worse than an absent one — the same rule that gates `p` to the pane.

    Off a console there is no published selection and no slot to hold, so the layer is not
    offered at all: `check_action` answers `False`, which is what `App.run_action` consults
    before dispatching a priority binding.
    """
    app = ProjectsPane(_context(_Listing(())))

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        assert app.check_action("chord", ("i",)) is False


async def test_an_alt_chord_from_a_pane_holding_no_console_slot_refuses_and_reads_nothing() -> None:
    """The strict read-gate the owner decided on, 2026-09-05.

    Two processes reach this line that are not one of the console's panes, and `hosting_mode`
    cannot tell either of them apart from a pane: a plain `remote-agents tui` started from any
    shell on the console's server, and the projects pane after a DEC-040 exchange parks it in an
    agent's own window. Both would otherwise answer from the *real* console's selection — the
    exact hazard the write side is gated against twice — and two of these keys end a session
    with no confirmation (DEC-018).

    The selection is never even read: the gate is asked first, so a refused process makes no
    claim on the console at all.
    """
    console = SelectionConsole(selected=SessionId.new(), holds_slot=False)
    app = ProjectsPane(
        replace(
            _context(_Listing(())),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        await pilot.press("alt+d")
        await pilot.pause()

        assert not isinstance(app.screen, SessionDetailScreen)
        assert console.reads == 0, "a process outside the console read its selection anyway"
        # The exact words, not merely that something was said. Telling this owner "no session is
        # selected" would send them to move a cursor that was never the problem — and that
        # distinction is the entire reason `_resolve_session` returns a pair instead of a value.
        assert announcements(app, severity="warning") == [
            "Session chords act on the console's own panes."
        ]


async def test_an_alt_chord_with_nothing_selected_says_so_and_navigates_nowhere() -> None:
    """DEC-027: a global binding warns on its own key rather than asking anything.

    The sessions cursor rests on nothing whenever the row it held has left the list, and that
    is published as nothing (Stage 2). A chord pressed in that window has no session to act on,
    and the honest answer is a word — not a guess at row 0, which is the whole hazard Stage 1
    closed.
    """
    console = SelectionConsole(selected=None)
    app = ProjectsPane(
        replace(
            _context(_Listing(())),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        before = console.reads
        await pilot.press("alt+d")
        await pilot.pause()

        assert not isinstance(app.screen, SessionDetailScreen)
        assert console.reads - before == 1
        # The other half of the pair: this owner *is* in a console pane, and the cursor really
        # is resting on nothing. Swap these two strings and both tests must fail.
        assert announcements(app, severity="warning") == ["No session is selected."]


async def test_a_screen_that_knows_its_own_session_answers_from_it() -> None:
    """`alt+c` on session A's detail must not act on session B.

    A detail, a rename and an inspect each name exactly one session, and none of them owns a
    cursor — so without this they resolve to whatever the sessions pane in the *other* pane
    happens to highlight. Orthogonal to the slot gate, and right even without it.
    """
    console = SelectionConsole(selected=SessionId.new())
    subject = str(SessionId.new())
    app = ProjectsPane(
        replace(
            _context(_Listing(())),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await app.push_screen(SessionDetailScreen(subject))
        await pilot.pause()
        before = console.reads

        assert await app.selected_session() == subject
        assert console.reads == before, "a screen that knows its session asked the console anyway"


async def test_a_screen_that_is_about_a_session_it_cannot_name_refuses_the_chord() -> None:
    """`InspectScreen` is about one session and holds no id for it — so it answers nothing.

    Its constructor takes the captured output alone; the session's name lives on the detail one
    level down the stack. Falling through to the published selection here would be the same
    defect as on the detail, one screen further along, so the declaration is what it is about
    rather than what it holds: `about_one_session` with no `subject_session` is a refusal.
    """
    console = SelectionConsole(selected=SessionId.new())
    app = ProjectsPane(
        replace(
            _context(_Listing(())),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await app.push_screen(InspectScreen("captured output"))
        await pilot.pause()
        before = console.reads

        assert await app.selected_session() is None
        assert console.reads == before


async def test_an_alt_chord_is_refused_while_a_modal_is_asking() -> None:
    """A modal is a question awaiting an answer, and the layer must not act behind it.

    The confirm modals carry a question string and no session id — deliberately, so the stage
    gate's registry sweep can construct each one with no arguments — so they cannot answer
    `subject_session` the way the detail does. The rule is therefore about the modal rather
    than about its subject: no chord fires while one is up, whichever modal it is.
    """
    console = SelectionConsole(selected=SessionId.new())
    app = ProjectsPane(
        replace(
            _context(_Listing(())),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.push_screen(ForceConfirmModal("Force stop this session?"))
        await pilot.pause()

        assert app.check_action("chord", ("f",)) is False


@pytest.mark.parametrize(
    ("surface", "wired", "offered"),
    [
        (ProjectsPane, True, True),
        (SessionsPane, True, True),
        (LimitsPane, True, True),
        (FeedPane, True, True),
        (ProjectsPane, False, False),
        (SessionsPane, False, True),
        (RemoteAgentsTui, True, False),
    ],
)
async def test_which_surfaces_are_offered_the_chord_layer(
    surface: type[RemoteAgentsTui], wired: bool, offered: bool
) -> None:
    """Every position the layer reaches, and the two it must not — asserted as one table.

    The four console panes offer it, which is the owner's ask. A console pane that is not on a
    console offers nothing, except the sessions pane: `alt+s` there does exactly what `s` there
    already does, so it needs no console at all.

    **`RemoteAgentsTui` is the row that matters, and it is `False` in both columns.** Its
    default screen is `DashboardScreen`, which sets `owns_session_cursor` and deliberately
    binds none of `a i r s c f m` (`dashboard.py`). DEC-062's position names `SessionsScreen`
    and `SessionsPaneScreen` and *only* those: it makes an unconfirmed `s`/`c` legal there,
    tied to `_draw_listing` resting a vanished row on nothing, and the dashboard is a third
    position that argument does not reach. Offering the chords there would carry two
    unconfirmed stops onto a cursor that is not even the focused widget — the sessions region
    sits passive while the projects list on the left holds the keyboard — which is the exact
    "acts on a session the owner is not looking at" this layer's gate exists to prevent.

    So the discriminator is **whether the screen already carries the row keys**, not whether it
    owns a cursor. Where the bare letter is already legal the chord adds no hazard; where it is
    not, the chord may not smuggle it in.
    """
    console = SelectionConsole(selected=SessionId.new())
    context = _context(_Listing(()))
    if wired:
        context = replace(
            context,
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    app = surface(context)

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        assert app.check_action("chord", ("s",)) is offered


async def test_a_chord_behind_a_modal_does_not_fire_when_the_key_is_really_pressed() -> None:
    """The modal refusal, driven through a keypress rather than through `check_action`.

    The refusal above asks `check_action` directly, which dies if the branch is deleted but
    asserts nothing about *when Textual consults it*. That premise is load-bearing and specific
    to this layer being `priority=True`: non-priority bindings walk `_modal_binding_chain`,
    which truncates at the modal, so under `priority=False` the App's chords would be
    unreachable behind a modal for free. Priority walks `reversed(_binding_chain)` from the App
    down and reaches them, which is exactly why the refusal has to exist — and why it has to be
    asserted against a real press.
    """
    console = SelectionConsole(selected=SessionId.new())
    app = ProjectsPane(
        replace(
            _context(_Listing(())),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.push_screen(ForceConfirmModal("Force stop this session?"))
        await pilot.pause()
        before = console.reads

        await pilot.press("alt+d")
        await pilot.pause()

        assert isinstance(app.screen, ForceConfirmModal), "a chord navigated out from under a modal"
        assert console.reads == before, "a chord behind a modal read the console's selection"


async def test_a_chord_on_a_detail_acts_on_that_detail_s_session_not_the_published_one() -> None:
    """The headline hazard, as a keypress: `alt+d` on session A's detail must not open B's.

    The resolver-level assertion next door proves `selected_session`; this proves the sentence
    the plan actually wrote down, which is about a key pressed on a screen that is displaying
    one session while another pane highlights a different one.
    """
    published = SessionId.new()
    console = SelectionConsole(selected=published)
    subject = str(SessionId.new())
    app = ProjectsPane(
        replace(
            _context(_Listing(())),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await app.push_screen(SessionDetailScreen(subject))
        await pilot.pause()
        before = console.reads

        await pilot.press("alt+d")
        await pilot.pause()

        assert isinstance(app.screen, SessionDetailScreen)
        assert app.screen.session_value == subject
        assert app.screen.session_value != str(published)
        assert console.reads == before


async def test_an_alt_stop_issues_one_graceful_stop_against_the_published_id() -> None:
    """`alt+s` from a pane with no sessions list of its own — the layer's whole point.

    This test replaces the one that recorded the opposite. Until Task 3.2 the handler existed
    only on `SessionsScreen`, so a stop chord pressed on any other pane resolved a session,
    passed every gate, posted `RowStopAction`, and the message bubbled to the App, found no
    handler and was dropped — silently, with no notification and no log. That intermediate was
    pinned deliberately so it could not be confused in CI with the working state; this is the
    working state it was waiting for.

    Asserting the *id* and not merely the count, because a chord acting on the projects pane's
    own rows instead of the published selection would also issue exactly one command.

    DEC-018: `s` does not ask, on any pane. There is no modal in this path and none is expected.
    """
    chosen = _record()
    console = SelectionConsole(selected=chosen.session_id)
    listing = _Listing((chosen,))
    app = ProjectsPane(
        replace(
            _context(listing),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        await pilot.press("alt+s")
        await pilot.pause()

        assert [type(command) for command in listing.issued] == [GracefulStopCommand]
        assert listing.issued[0].session_id == chosen.session_id
        assert not announcements(app, severity="warning")
        # DEC-018 as the thing tested rather than as a corollary: issuing a command would also
        # be the outcome of a modal that had been answered `True`, so the absence of the modal
        # is asserted rather than inferred from the presence of the stop.
        assert not isinstance(app.screen, ForceConfirmModal), "a graceful stop asked first"


async def test_an_alt_stop_asks_for_force_from_the_receiving_pane_and_escape_dismisses_it() -> None:
    """`alt+f` in a pane that has never had a sessions list draws the modal on its own pump.

    **This is the shape DEC-025 and DEC-068 require, and the reason `perform_row_action` posts
    rather than performs.** A binding action runs on the *App's* message pump, so awaiting a
    modal there stops the app draining messages — measured on the owner's real workstation
    before this stage: the modal drew, then Escape did nothing, `ctrl+q` did nothing, and the
    process had to be killed. Posting `RowStopAction` to the receiving screen puts the question
    on that screen's pump, where a suspension holds back only that screen's events.

    So the assertion that matters is not merely that the modal appeared: it is that **the app
    is still answering afterwards**. Escape must dismiss it and the surface must still respond
    to a further key.

    DEC-018 draws the line this test sits on: `s` and `c` never ask, `f` always does.
    """
    chosen = _record()
    console = SelectionConsole(selected=chosen.session_id)
    listing = _Listing((chosen,))
    app = FeedPane(
        replace(
            _context(listing),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        await pilot.press("alt+f")
        await pilot.pause()

        assert isinstance(app.screen, ForceConfirmModal), "force did not ask before killing"

        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, ForceConfirmModal), "escape did not dismiss the modal"
        assert listing.issued == [], "an aborted confirmation issued a stop anyway"
        assert app.is_running, "the surface stopped answering after the modal"


async def test_an_alt_stop_from_a_console_pane_never_reaches_that_pane_s_own_rows() -> None:
    """The projects pane's rows are projects, and a stop chord must not touch them.

    Stated as its own case because the failure it guards is silent: `perform_row_action` takes
    the session it is given, and a resolver that fell back to "whatever this screen highlights"
    would hand it a project id, which the store would answer `None` for — a refusal that looks
    like an ordinary vanished session rather than like a bug.

    `PRESERVED` rather than `RUNNING`, because `alt+c` is the chord under test and cleanup is
    only available on a preserved session — `tui.stop` re-reads the record and re-checks the
    policy at issue time (DEC-007), so a running one is refused before the pane's rows are ever
    relevant and the test would pass for the wrong reason.
    """
    chosen = _record(SessionState.PRESERVED)
    console = SelectionConsole(selected=chosen.session_id)
    listing = _Listing((chosen,))
    app = ProjectsPane(
        replace(
            _context(listing),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        rows = app.screen.query_one("#choices", OptionList)
        assert rows.option_count, "the projects pane drew no rows, so this proves nothing"

        await pilot.press("alt+c")
        await pilot.pause()

        assert [type(command) for command in listing.issued] == [CleanupCommand]
        assert listing.issued[0].session_id == chosen.session_id
        assert not isinstance(app.screen, ForceConfirmModal), "a cleanup asked first"


async def test_the_standalone_sessions_position_is_offered_the_chord_layer_off_a_console() -> None:
    """`ctrl+s` from the plain dashboard reaches `SessionsScreen`, a different class from the pane.

    It is the other holder of `carries_row_keys`, and the table above never reaches it — the
    panes rest on `SessionsPaneScreen` and the plain surface rests on `DashboardScreen`. Here
    the bare letters are bound and legal (DEC-062 names this position by name), so `alt+s` is
    the same act on the same cursor and needs no console at all.
    """
    app = RemoteAgentsTui(_context(_Listing((_record(),))))

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert app.check_action("chord", ("s",)) is False, "the dashboard offers no chords"

        await app.action_sessions()
        await pilot.pause()

        assert isinstance(app.screen, SessionsScreen)
        assert app.check_action("chord", ("s",)) is True


async def test_a_chord_stands_down_when_a_command_starts_during_the_gate_read() -> None:
    """The post-await re-check, which the review found had no killing test.

    `busy` is checked before `_resolve_session` and the gate then makes up to two tmux
    subprocess round trips, so a command starting inside that window would otherwise be
    overtaken. The row keys cannot hit this — they check `busy` with no await between the check
    and the use — so the chord is the one path that could navigate while a command is in
    flight, and `perform_row_remote_control` reaches `show_detail`, which `tui.stop`'s own
    refusal does not cover.

    Driven by making the gate itself take the surface busy, which is exactly the interleaving:
    the answer the chord is waiting for is the thing that arrives too late.
    """
    console = SelectionConsole(selected=SessionId.new())
    app = ProjectsPane(_context(_Listing(())))

    async def gate_that_starts_a_command() -> bool:
        app.set_busy(True)
        return True

    app._services = replace(
        app.services,
        console_read_selection=console.read,
        console_holds_slot=gate_that_starts_a_command,
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        await pilot.press("alt+d")
        await pilot.pause()

        assert not isinstance(app.screen, SessionDetailScreen), (
            "a chord navigated while a command was in flight"
        )


async def test_alt_force_on_a_session_detail_asks_about_that_detail_s_own_session() -> None:
    """The signature the inherited handler calls with — a `TypeError` here exits the app.

    `ChoiceScreen.on_row_stop_action` calls `self.confirm_force(message.session_value)`, and
    `SessionDetailScreen` overrides `confirm_force`. Before the override took the argument, the
    first `alt+f` pressed on any detail raised `TypeError` out of a message handler, which
    Textual turns into `App._handle_exception` and the surface ends. Reachable by two keys from
    any pane — `alt+d`, then `alt+f` — and by nothing else in the suite, which is why no test
    saw it until a review read the signatures.

    The modal has to name *this* detail's session, not the published one, which is the same
    property `subject_session` exists for one layer down.

    **Everything is captured inside the block and asserted outside it**, which is the shape
    `test_tui_force_stop.py` already uses and is load-bearing rather than stylistic: an
    assertion raised while the confirmation is still up leaves its worker awaiting an answer
    that never comes, and `run_test`'s teardown then waits for that worker forever — so a
    failing assertion presents as a hung suite rather than as a failure. Escape first, assert
    after.
    """
    # Distinguished by *sequence*, not by project: `with_project_names` resolves every record's
    # slug to the catalogue's one name before it is rendered, so a per-record project string
    # never reaches the modal and an assertion built on one is unfalsifiable.
    published = _record(ordinal=1)
    subject = _record(ordinal=2)
    console = SelectionConsole(selected=published.session_id)
    listing = _Listing((published, subject))
    app = ProjectsPane(
        replace(
            _context(listing),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await app.push_screen(SessionDetailScreen(str(subject.session_id)))
        await pilot.pause()

        await pilot.press("alt+f")
        await pilot.pause()

        asked = position(app)
        question = getattr(app.screen, "_question", "")
        alive = app.is_running

        await pilot.press("escape")
        await pilot.pause()
        after = position(app)

    assert alive, "alt+f on a detail took the surface down"
    assert asked == "FORCE_MODAL", f"alt+f on a detail reached {asked} instead of the confirmation"
    named_subject, named_published = with_project_names((subject, published), (_EXISTING,))
    assert named_subject.display.rendered in question, (
        "the modal named a session this detail is not showing"
    )
    assert named_published.display.rendered not in question
    assert listing.issued == [], "an aborted confirmation force-stopped the session"
    assert after == "SESSION_DETAIL", f"aborting returned to {after}, not the detail"


async def test_a_chord_stop_leaves_the_projects_filter_exactly_as_the_owner_left_it() -> None:
    """The owner's originating ask, on the success path — and it was broken by the handler move.

    `tui.stop` calls back into the receiving screen when the stop succeeds. That callback used
    to mean "re-read yourself", which is right for the two sessions positions and destructive
    on the projects pane: `on_reveal` there calls `render_projects()`, which clears the filter
    `Input` and moves focus off it. So `/`, `ab`, `M-s` would stop the right session and then
    silently wipe the query and hand the keyboard to the rows — after which the next bare `o`
    toggles the project order instead of typing a letter.

    That is exactly the guarantee Task 3.4's live case asserts and exactly the ask this stage
    exists to satisfy, so it is pinned here at unit speed rather than only in a live test that
    does not run on commit.
    """
    chosen = _record()
    console = SelectionConsole(selected=chosen.session_id)
    listing = _Listing((chosen,))
    app = ProjectsPane(
        replace(
            _context(listing),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press("/")
        await pilot.press(*"ab")
        await settle_filter(pilot)
        entry = app.screen.query_one("#filter", Input)
        assert entry.value == "ab" and entry.has_focus

        await pilot.press("alt+s")
        await pilot.pause()

        assert [type(command) for command in listing.issued] == [GracefulStopCommand]
        assert entry.value == "ab", "the stop cleared the filter the owner had typed"
        assert entry.has_focus, "the stop moved the keyboard off the filter"


async def test_a_failed_chord_stop_leaves_a_non_sessions_pane_s_rows_alone() -> None:
    """A stop that raises must not replace the projects catalogue with a lone `Back` row.

    `redraw_after_failure`'s base implementation collapses the list and writes "Go back and open
    the session again to see its current state" — right for a screen describing one record, and
    on the projects pane a position with no session and nothing to go back to. Its own docstring
    was written against precisely this shape of defect on a listing; the handler move made it
    reachable on three more listings.

    The failure is still *reported*: `stop` raises a toast regardless, which is what a pane with
    nothing to redraw has instead of a redraw.
    """
    chosen = _record()
    console = SelectionConsole(selected=chosen.session_id)
    listing = _Listing((chosen,), error=RuntimeError("tmux went away"))
    app = ProjectsPane(
        replace(
            _context(listing),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press("/")
        await pilot.press(*"ab")
        await settle_filter(pilot)
        entry = app.screen.query_one("#filter", Input)
        rows = app.screen.query_one("#choices", OptionList)
        before = [option.id for option in rows.options]
        assert before, "the projects pane drew no rows, so this proves nothing"

        await pilot.press("alt+s")
        await pilot.pause()

        assert [option.id for option in rows.options] == before, (
            "a failed stop replaced the project catalogue with the detail's lone Back row"
        )
        # The ids alone cannot see a redraw that re-renders the *same* catalogue, which is what
        # `refuse()` -> `render_projects()` does — so the filter and the focus are asserted here
        # too. They are the discriminators that tell "nothing happened" from "re-rendered
        # identically", and without them this case is blind to the whole refusal family.
        assert entry.value == "ab" and entry.has_focus
        assert announcements(app), "a failed stop said nothing at all"


@pytest.mark.parametrize(
    ("name", "state", "key", "records", "said"),
    [
        (
            "the policy refuses the action on this state",
            SessionState.RUNNING,
            "alt+c",
            True,
            "is no longer available for this session",
        ),
        (
            "the session ended between the publish and the press",
            SessionState.RUNNING,
            "alt+s",
            False,
            "That session is no longer available.",
        ),
    ],
)
async def test_a_refused_chord_stop_leaves_a_non_sessions_pane_untouched_and_says_so(
    name: str, state: SessionState, key: str, records: bool, said: str
) -> None:
    """`stop`'s own refusals, which the first fix routed around rather than through.

    Two branches, both one keypress from the console's projects pane, and both reached far more
    often than the failure path:

    * **the policy refuses** — `alt+c` against a RUNNING selection, since cleanup is offered only
      on PRESERVED. `resolve_stop` answers `UNAVAILABLE` before anything is dispatched.
    * **the session is gone** — `alt+s` against a selection whose row has ended, which is exactly
      the window DEC-007's re-read exists to catch.

    Both used to call `ChoiceScreen.refuse`, which is announce-then-`on_reveal` — so on the
    projects pane they re-rendered the catalogue, cleared the filter and moved the keyboard off
    it. The vanished branch passes no message, so that one also said *nothing*: a chord that ate
    the owner's typing and reported nothing at all.

    The assertions are the discriminators, not the rows: `refuse` re-renders the *same* project
    ids, so a test comparing ids alone cannot see it.
    """
    chosen = _record(state)
    console = SelectionConsole(selected=chosen.session_id)
    listing = _Listing((chosen,) if records else ())
    app = ProjectsPane(
        replace(
            _context(listing),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press("/")
        await pilot.press(*"ab")
        await settle_filter(pilot)
        entry = app.screen.query_one("#filter", Input)
        assert entry.value == "ab" and entry.has_focus

        await pilot.press(key)
        await pilot.pause()

        assert listing.issued == [], f"{name}: a refused stop was issued anyway"
        assert entry.value == "ab", f"{name}: the refusal cleared the filter"
        assert entry.has_focus, f"{name}: the refusal moved the keyboard off the filter"
        # **The sentence, not merely that a sentence appeared.** The two branches say different
        # things on purpose — `refuse_stop` argues at length that the vanished case must supply
        # its own words *because* there is no redraw to supply them — and an unfiltered
        # `announcements(app)` is true whichever message fired, so the two could be swapped and
        # both parameters would stay green. The identical correction was made one task earlier;
        # this is the same shape reappearing, which is why it is spelled out here.
        assert any(said in message for message in announcements(app, severity="warning")), (
            f"{name}: expected the refusal to say {said!r}, got {announcements(app)}"
        )


async def test_a_stop_that_did_not_take_does_not_leave_its_sentence_on_a_projects_pane() -> None:
    """The fourth callback on the seam, and the one whose absence would be permanent.

    A graceful stop that dispatches and does not take effect is an ordinary outcome, not an
    error path. `stop` reports it twice on purpose — once in the status line, once as a toast —
    and the status line is the position's own description of itself. On the sessions pane the
    line is replaced moments later by `after_stop`'s re-read; on the projects pane `after_stop`
    is deliberately a no-op, so nothing ever puts it back. Writing there would strand
    "Graceful stop did not take effect…" where "Choose a project" belongs, for the rest of the
    pane's life.

    The toast is asserted alongside, because the point is not that the failure goes unreported —
    it is that it is reported in the sink that is not this position's self-description.
    """
    chosen = _record()
    console = SelectionConsole(selected=chosen.session_id)
    listing = _Listing((chosen,), observation=a_stop_that_did_not_take("the pane is still alive"))
    app = ProjectsPane(
        replace(
            _context(listing),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        before = status(app)

        await pilot.press("alt+s")
        await pilot.pause()

        assert [type(command) for command in listing.issued] == [GracefulStopCommand]
        assert status(app) == before, (
            "the stop's outcome was written into a status line that describes the project list"
        )
        assert any("did not" in said or "still alive" in said for said in announcements(app)), (
            "the stop that did not take effect was not reported at all"
        )


async def test_an_unreadable_store_does_not_repaint_a_pane_that_shows_no_sessions() -> None:
    """`report_store_failure` is the last callback on the seam, reached by a chord's own re-read.

    Every chord re-reads the record before acting (DEC-007), and that read can fail — it is a
    store read from a keypress. The reporting was written for the positions that list sessions:
    it replaces the rows with a lone `Back` and writes "The managed sessions could not be read"
    into the status line. On the projects pane both sentences are about something that position
    does not show, and the row replacement destroys the catalogue it does.

    Driven through `alt+m`, which is the chord that re-reads and then decides what its key means
    — the one place a chord asks the store a question before it knows what it is doing.

    The toast still fires, which is the whole distinction being drawn: the failure is reported,
    it just does not get to rewrite a listing it is not about.
    """
    chosen = _record()
    console = SelectionConsole(selected=chosen.session_id)
    listing = _Listing((chosen,), read_error=RuntimeError("the database is locked"))
    app = ProjectsPane(
        replace(
            _context(listing),
            console_read_selection=console.read,
            console_holds_slot=console.holds_console_slot,
        )
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        rows = app.screen.query_one("#choices", OptionList)
        before_rows = [option.id for option in rows.options]
        before_status = status(app)
        assert before_rows, "the projects pane drew no rows, so this proves nothing"

        await pilot.press("alt+m")
        await pilot.pause()

        assert [option.id for option in rows.options] == before_rows, (
            "an unreadable sessions store replaced the project catalogue with a lone Back row"
        )
        assert status(app) == before_status, (
            "an unreadable sessions store rewrote a status line describing the project list"
        )
        assert any("could not be read" in said for said in announcements(app)), (
            "the unreadable store was not reported at all"
        )
