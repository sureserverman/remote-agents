"""The right-top pane: every managed session, and the pane a session is opened from.

The sessions pane is the swap controller deliberately. It is the one pane that stays on
screen once an agent occupies the left slot, so it is the only place the owner can reach
back from — which is why Enter here means *exchange this agent into the left pane*, and the
detail, where every stop, inspect, rename and Remote Control affordance lives, is one key
away (DEC-007: the full action set stays reachable; opening narrows nothing).

The list, its own refresh cadence, the stale-read guards and the empty state are inherited
from `SessionsScreen` rather than re-implemented — what this pane changes is what Enter
means. On the combined dashboard Enter on a session row already opened the session and `d`
already opened the detail; this is that pair, on a screen of its own.
"""

from __future__ import annotations

from datetime import UTC, datetime

from backends import tui_context_for
from textual.widgets import OptionList
from tui_positions import position

from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.panes import SessionsPane
from remote_agents.adapters.tui.screens.sessions import SessionDetailScreen, SessionsPaneScreen
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
_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_OTHER = SessionId.parse("fedcba98-7654-3210-fedc-ba9876543210")


class _Launcher:
    def __init__(self, records: tuple[SessionRecord, ...]) -> None:
        self.records = records

    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records


def _record(session_id: SessionId = _SESSION, name: str = "existing") -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity(name, "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


def _context(records: tuple[SessionRecord, ...] = (), **overrides) -> TuiContext:
    base = {
        "sessions": _Launcher(records),
        "projects": object(),
        "profiles": (ProfileAvailability("claude", True),),
        "refresh_catalogue": lambda: (_PROJECT,),
        "attach_argv": lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        "catalogue": (_PROJECT,),
    }
    base.update(overrides)
    return tui_context_for(**base)


def test_the_sessions_pane_rests_on_the_sessions_list() -> None:
    assert isinstance(SessionsPane(_context()).get_default_screen(), SessionsPaneScreen)


async def test_enter_on_a_row_issues_one_show_and_the_pane_stays() -> None:
    """Enter exchanges the agent into the left slot; the pane it was pressed from remains.

    One call, not two: the pane is the controller and a doubled exchange would swap the
    agent in and straight back out again.
    """
    shown: list[str] = []

    async def show(session_id: str) -> None:
        shown.append(session_id)

    app = SessionsPane(_context((_record(),), open_in_console=show))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert choices.highlighted == 0
        choices.focus()
        await pilot.press("enter")
        await pilot.pause()

        assert shown == [str(_SESSION)]
        assert app.is_running, "opening a session never ends the pane that opened it"
        assert position(app) == "SESSIONS_PANE"


async def test_enter_opens_the_row_the_cursor_is_on_not_the_first_one() -> None:
    shown: list[str] = []

    async def show(session_id: str) -> None:
        shown.append(session_id)

    records = (_record(), _record(_OTHER, "other"))
    app = SessionsPane(_context(records, open_in_console=show))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.focus()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert shown == [str(_OTHER)]


async def test_the_detail_key_opens_the_detail_with_no_prior_arrow_press() -> None:
    """DEC-007's full action set is one key away, and it answers a bare key.

    A bare `d` rather than a hand-set `highlighted`: a pane advertising a key that only works
    after an arrow press makes it a silent no-op, which is invisible to a test that sets the
    cursor itself.
    """
    app = SessionsPane(_context((_record(),)))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#choices", OptionList).focus()
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, SessionDetailScreen)


async def test_an_empty_list_declares_its_state() -> None:
    """DEC-009: this pane can be empty, and says so rather than showing nothing."""
    app = SessionsPane(_context(()))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert choices.option_count == 1
        option = choices.get_option_at_index(0)
        assert option.disabled is True
        assert "No managed sessions on this host." in str(option.prompt)


async def test_the_cursor_rests_painted_on_a_row_whose_enter_does_not_mutate() -> None:
    """BL-004's constraint, as it lands on this pane.

    The resting row's Enter must not mutate, and here it does not: Enter *opens* — an
    exchange of panes, which writes no record and touches no lifecycle (DEC-040). Every
    mutating action lives behind `d`, on the detail. So the cursor may rest on the first
    session row, which is what the combined dashboard's pane already does, and the row it
    rests on must be *painted* rather than merely indexed — the distinction
    `test_resting_cursor.py` exists for.
    """
    shown: list[str] = []

    async def show(session_id: str) -> None:
        shown.append(session_id)

    app = SessionsPane(_context((_record(),), open_in_console=show))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert choices.highlighted == 0
        cursor = choices.get_visual_style(
            "option-list--option", "option-list--option-highlighted"
        ).rich_style.clear_meta_and_links()
        painted = [
            segment
            for line in range(choices.scrollable_content_region.height)
            for segment in choices.render_line(line)
            if segment.text.strip() and segment.style is not None
        ]
        assert any(segment.style.clear_meta_and_links() == cursor for segment in painted), (
            "the sessions pane drew no cursor on its resting row"
        )

        choices.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert shown == [str(_SESSION)], "the resting row's enter must open, never mutate"


async def test_the_pane_re_reads_on_its_own_cadence() -> None:
    """The store has a second writer, so this pane goes stale with nobody touching it."""
    app = SessionsPane(_context((_record(),)))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert app.screen._auto is not None, "the sessions pane must poll its own list"


async def test_without_a_console_capability_opening_still_leaves_by_attach() -> None:
    """A pane run outside the console keeps the exec-attach contract exactly as it was."""
    app = SessionsPane(_context((_record(),), open_in_console=None))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.focus()
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value is not None
    assert app.return_value.session_id == str(_SESSION)


async def test_the_console_capability_the_composition_wires_is_the_exchange() -> None:
    """The wiring itself, at the seam the composition root owns.

    Asserted against the executed capability rather than bootstrap's source text, for the
    reason `test_tui_bootstrap.py` records: a substring check for the same wiring matched the
    *service* composition too, so deleting it from the local one left the suite green.

    DEC-039's accepted cost 1 names this replacement by hand — under the swap model the
    console reaches an agent through `ConsoleComposer.show`, and Sub-plan 3 wires that in
    place of the switch route.
    """
    from remote_agents.bootstrap import _console_opener

    class _Composer:
        def __init__(self) -> None:
            self.shown: list[SessionId] = []
            self.opened: list[SessionId] = []

        async def show(self, session_id: SessionId) -> None:
            self.shown.append(session_id)

        async def open(self, session_id: SessionId) -> None:  # pragma: no cover - must not run
            self.opened.append(session_id)

    composer = _Composer()
    await _console_opener(composer)(str(_SESSION))
    assert composer.shown == [_SESSION]
    assert composer.opened == []


async def test_a_show_that_fails_leaves_the_pane_running_and_says_so() -> None:
    """Presentation degrades; the pane never dies because an exchange did not happen."""

    async def refuse(session_id: str) -> None:
        raise RuntimeError("the console is wedged")

    app = SessionsPane(_context((_record(),), open_in_console=refuse))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.is_running
        assert position(app) == "SESSIONS_PANE"


async def test_the_status_names_what_enter_actually_does_here() -> None:
    """Inherited, both sentences described the *dashboard's* keys.

    Found by driving the real pane at the Stage 1 gate: it read "Select one for detail",
    which is what Enter means on the sessions screen the dashboard pushes and not what it
    means here. A status that names the wrong key is a false sentence, and it is the kind
    only a live drive shows.
    """
    from textual.widgets import Static

    app = SessionsPane(_context((_record(),)))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        status = str(app.screen.query_one("#status", Static).content)
        assert "Enter opens one" in status
        assert "d for its detail" in status
        assert "Select one for detail" not in status


async def test_an_empty_pane_does_not_offer_an_escape_it_does_not_have() -> None:
    """This pane is its process's resting position, so escape at rest is inert.

    The inherited sentence sent the owner to a project list that does not exist in this
    process — a dead end dressed as an instruction.
    """
    from textual.widgets import Static

    app = SessionsPane(_context(()))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        status = str(app.screen.query_one("#status", Static).content)
        assert "escape" not in status.lower()
        assert "project list" not in status


async def test_a_failed_read_does_not_send_the_owner_somewhere_that_is_not_there() -> None:
    """The failure path kept the sentence the gate commit fixed everywhere else.

    `report_store_failure` renders onto the screen whose read failed. On this pane that
    screen is the process's resting position: `go_back` refuses to pop the last screen, so
    "Press escape to return to the project list" named an inert key and a position that does
    not exist in this process — and it drew a Back row that could not go back, at the moment
    the surface most needed to be honest. Found by the Stage 1 gate evaluator.
    """
    from textual.widgets import OptionList, Static

    class _Failing(_Launcher):
        async def list_sessions(self):
            raise RuntimeError("store contended")

    app = SessionsPane(_context((), sessions=_Failing(())))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        # Through the screen's own reload, which is the path that catches and reports.
        await app.screen.reload()
        await pilot.pause()

        status = str(app.screen.query_one("#status", Static).content)
        assert "could not be read" in status, "the failure must still be named"
        assert "escape" not in status.lower()
        assert "project list" not in status
        assert "Ctrl+R" in status

        choices = app.screen.query_one("#choices", OptionList)
        rows = [str(option.prompt) for option in choices.options]
        assert "Back" not in rows, "a Back row that cannot go back is a key that does nothing"


async def test_the_pane_offers_no_flow_that_starts_by_choosing_a_project() -> None:
    """Carried from the Stage 1 gate: every pane inherited the whole app's bindings.

    All three flows — add project, resume, sessions — begin by choosing a project, which is
    the pane next door. Pushing the launch wizard in here would bury the list this pane
    exists to keep in sight, and "Sessions" is a key for reaching a list that is already on
    screen. Hidden *and* declined: this surface's rule is that a footer entry may only be
    hidden where the action it names already refuses to run.
    """
    app = SessionsPane(_context((_record(),)))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        offered = set(app.screen.active_bindings)
        assert {"ctrl+n", "ctrl+o", "ctrl+s"}.isdisjoint(offered), offered

        await app.action_add_project()
        await app.action_sessions()
        await pilot.pause()
        assert position(app) == "SESSIONS_PANE", "a declined flow must not move the pane"


async def test_a_session_that_cannot_be_shown_says_why_instead_of_doing_nothing() -> None:
    """The bug an owner actually hit: click a row, watch nothing happen.

    `ConsoleComposer.show` degrades to a log line by contract (DEC-040) and nothing in
    `src/` configures logging, so a session it declined to display was silence. The
    commonest reason is not a fault: a session launched before identity moved to the pane
    (DEC-038) names no pane, so there is nothing to exchange. It is still listed, stoppable
    and inspectable — it just cannot be shown, and now it says so and names the repair.
    """

    async def refuse(session_id: str) -> str:
        return "This session started before ... Run: remote-agents upgrade-sessions"

    app = SessionsPane(_context((_record(),), open_in_console=refuse))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.focus()
        await pilot.press("enter")
        await pilot.pause()

        said = [str(note.message) for note in app._notifications]
        assert any("upgrade-sessions" in line for line in said), said
        assert app.is_running, "a refusal is not a reason to lose the pane"


async def test_a_session_that_is_shown_says_nothing_at_all() -> None:
    """Success is silent; only a refusal is worth interrupting for."""

    async def show(session_id: str) -> None:
        return None

    app = SessionsPane(_context((_record(),), open_in_console=show))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        choices.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert list(app._notifications) == []


# The project a row names ----------------------------------------------------------------------


async def test_a_row_names_its_project_rather_than_the_catalogue_id() -> None:
    """The defect this closes, captured from the live surface before the change:

        034b69be3a8290521db3d76e · codex · regular · #3 · running · 10d ago

    `SessionDisplayIdentity.project_slug` holds the catalogue's `opaque_id`, and the bot has
    always swapped it for the readable name at render time. This surface never did.
    """
    app = SessionsPane(_context((_record(name="opaque-existing"),)))
    async with app.run_test() as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        row = choices.get_option_at_index(0)
        assert "existing" in str(row.prompt)
        assert "opaque-existing" not in str(row.prompt)


async def test_naming_the_project_leaves_the_row_key_alone() -> None:
    """The key is the handle every action screen is reached through.

    Getting the name wrong is cosmetic; getting the *key* wrong strands Stop, Force stop,
    Rename and Inspect behind a row that no longer addresses anything.
    """
    app = SessionsPane(_context((_record(name="opaque-existing"),)))
    async with app.run_test() as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert choices.get_option_at_index(0).id == str(_SESSION)


async def test_a_session_whose_project_left_the_catalogue_still_renders() -> None:
    """Deregistered, or a directory moved, while the session runs. The slug is then the only
    name there is, and a row the owner cannot see is a session they cannot stop."""
    app = SessionsPane(_context((_record(name="vanished"),), catalogue=()))
    async with app.run_test() as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert choices.option_count == 1
        assert "vanished" in str(choices.get_option_at_index(0).prompt)
