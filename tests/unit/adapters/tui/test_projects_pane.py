"""The left pane: the projects surface on its own, without the two right-hand regions.

The combined dashboard put projects, sessions and the feed in one Textual app. Under the
three-pane console the left pane is its own process, and what it must still be is the whole
projects position — the catalogue, the filter and its debounce, the Launch/Resume chooser,
and the flows those push. So this pane *subclasses* the projects picker and the dashboard
subclasses the pane, which is what keeps one implementation of "choosing a project opens the
chooser" rather than two that drift.

What is pinned here is the pane's own shape: it rests on the projects position, it has no
sessions or feed region, the filter narrows, the chooser opens, Launch reaches the agent
list, its empty state is declared (DEC-009), and the cursor rests drawn on a non-mutating
row with the keyboard in the filter, which is what `test_resting_cursor.py` requires of every
list this surface shows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from backends import SessionUseCaseDouble, tui_context_for
from textual.widgets import Input, OptionList, Static
from tui_filter import settle_filter
from tui_positions import position

from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.panes import ProjectsPane
from remote_agents.adapters.tui.preferences import (
    ALPHABETICAL,
    RECENCY,
    read_project_order,
    write_project_order,
)
from remote_agents.adapters.tui.screens.dashboard import (
    ProjectChooserScreen,
    ProjectsPaneScreen,
)
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_admin import CreatedProject, CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.projects import ProjectIdentity
from remote_agents.ports.session_store import ProjectUsage

_INFRA = CatalogProject("opaque-infra", "remote-agents", "infra", "Registered")
_TOOLS = CatalogProject("opaque-tools", "opaque-shift", "dev-area", "Registered")
_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")


class _Creator:
    def available_areas(self) -> tuple[str, ...]:
        return ("dev-area", "infra")

    def create(self, command: CreateProjectCommand) -> CreatedProject:
        identity = ProjectIdentity(area=command.area, name=command.name)
        return CreatedProject(identity, Path("/dev") / command.area / command.name)


class _Launcher(SessionUseCaseDouble):
    """A host with launch history, or with none -- `project_usage` is a render-time read.

    Inherits the three reads a screen makes while drawing rather than restating them: the
    projects pane now asks `project_usage` on every catalogue refresh, so a double that
    answered only `list_sessions` would fail at the first draw for a reason no test here is
    about.
    """

    def __init__(
        self,
        records: tuple[SessionRecord, ...] = (),
        usage: tuple[ProjectUsage, ...] = (),
    ) -> None:
        self.records = records
        self.usage = usage
        #: How many times the catalogue's order was actually recomputed.
        self.usage_reads = 0

    async def project_usage(self) -> tuple[ProjectUsage, ...]:
        self.usage_reads += 1
        return self.usage

    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records


def _record() -> SessionRecord:
    return SessionRecord(
        _SESSION,
        ProjectId("opaque-infra"),
        ProfileId("claude"),
        SessionDisplayIdentity("remote-agents", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


def _context(**overrides) -> TuiContext:
    base = {
        "sessions": _Launcher((_record(),)),
        "projects": _Creator(),
        "profiles": (ProfileAvailability("claude", True),),
        "refresh_catalogue": lambda: (_INFRA, _TOOLS),
        "attach_argv": lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        "catalogue": (_INFRA, _TOOLS),
    }
    base.update(overrides)
    return tui_context_for(**base)


def test_the_projects_pane_rests_on_the_projects_position() -> None:
    assert isinstance(ProjectsPane(_context()).get_default_screen(), ProjectsPaneScreen)


async def test_the_pane_has_neither_right_hand_region() -> None:
    """The two regions are other processes now; a copy of them here would be a second one."""
    app = ProjectsPane(_context())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert position(app) == "PROJECTS"
        assert not app.screen.query("#sessions-pane")
        assert not app.screen.query("#feed-pane")


async def test_the_filter_narrows_the_catalogue() -> None:
    app = ProjectsPane(_context())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert choices.option_count == 2
        app.screen.render_projects("opaque-shift")
        await pilot.pause()
        assert [option.id for option in choices.options] == ["opaque-tools"]


async def test_choosing_a_project_opens_the_chooser() -> None:
    """Not straight into the agent list: Launch and Resume are one question per project."""
    app = ProjectsPane(_context())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await app.screen.choose("opaque-infra")
        await pilot.pause()
        assert isinstance(app.screen, ProjectChooserScreen)
        assert position(app) == "PROJECT_CHOOSER"


async def test_launch_from_the_chooser_reaches_the_agent_list() -> None:
    app = ProjectsPane(_context())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await app.screen.choose("opaque-infra")
        await pilot.pause()
        await app.screen.choose("launch")
        await pilot.pause()
        assert position(app) == "PROFILES"
        assert app.selection.project == _INFRA


async def test_the_pane_declares_its_empty_state() -> None:
    """DEC-009: a filter that matches nothing says so rather than showing a blank list."""
    app = ProjectsPane(_context())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.screen.render_projects("nothing-matches-this")
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert choices.option_count == 1
        option = choices.get_option_at_index(0)
        assert option.disabled is True
        assert "No project matches that filter." in str(option.prompt)


async def test_the_keyboard_rests_in_the_filter_and_one_down_draws_the_cursor() -> None:
    """The resting-cursor discipline as this position actually holds it.

    The projects list renders with `focus=False`, so it deliberately draws **no** cursor while
    it is resting: the keyboard is in the filter, where typing narrows instead of being
    swallowed, and a highlighted row under a keyboard that is somewhere else would advertise
    an enter target the owner cannot reach. The discipline that does bind here is the one
    BL-004 states for the sessions pane — **one** key from the resting position reaches the
    first row — and from there the cursor must be *painted*, which is the half that was
    missing in the defect `test_resting_cursor.py` was written for. So this asserts the
    rendered highlight rather than the index, exactly as that file insists.
    """
    app = ProjectsPane(_context())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert isinstance(app.focused, Input)
        assert app.focused.id == "filter"
        assert choices.highlighted is None, "a resting list must not advertise an enter target"

        await pilot.press("down")
        await pilot.pause()
        assert app.focused is choices
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
            "the projects pane drew no cursor on the row the keyboard had reached"
        )


@pytest.mark.parametrize("nothing", [()])
async def test_an_empty_catalogue_still_rests_somewhere(nothing) -> None:
    app = ProjectsPane(_context(refresh_catalogue=lambda: nothing, catalogue=nothing))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert position(app) == "PROJECTS"
        assert app.is_running


# --- The start-time recovery report reaches the owner (Task 1.5) -----------------------
#
# `ConsoleComposer.settle()` is the console's start-only repair, and only the process
# resident in the left slot may run it — which, under the three-pane console, is this pane.
# What it returns used to go nowhere: `moved` was logged at INFO with no logging configured
# anywhere in `src/`, and `blocked` was printed to stderr in the instant before Textual took
# the alternate screen, invisible for the whole session it described.
#
# The two halves get different destinations, because they are different facts.
# `moved` is something that already happened and is done — a confirmation, which this
# surface's own rule puts in a toast. `blocked` is what needs a *person*, and this surface's
# rule for anything the owner must keep is the status line. So a blocked note stands there
# rather than expiring, because nothing in this process is going to fix it.

from remote_agents.application.console import RecoveryReport  # noqa: E402


def _recovered(*, moved=(), blocked=(), settled=True) -> RecoveryReport:
    return RecoveryReport(tuple(moved), tuple(blocked), settled=settled)


async def test_a_recovery_that_moved_something_tells_the_owner_it_happened() -> None:
    app = ProjectsPane(
        _context(console_recovery=_recovered(moved=("the projects surface came home",)))
    )
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        said = [str(notification.message) for notification in app._notifications]
        assert any("the projects surface came home" in line for line in said)


async def test_a_blocked_recovery_note_stands_in_the_status_and_survives_a_refresh() -> None:
    """It needs a person, and nothing in this process is going to fix it."""
    app = ProjectsPane(
        _context(console_recovery=_recovered(blocked=("an agent is parked in a foreign pane",)))
    )
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        status = app.screen.query_one("#status", Static)
        assert "an agent is parked in a foreign pane" in str(status.content)

        app.screen.render_projects()
        await pilot.pause()
        assert "an agent is parked in a foreign pane" in str(status.content), (
            "a condition that still holds must not be redrawn away by an ordinary refresh"
        )


async def test_a_recovery_that_settled_cleanly_says_nothing_extra() -> None:
    app = ProjectsPane(_context(console_recovery=_recovered()))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert list(app._notifications) == []
        assert "Choose a project" in str(app.screen.query_one("#status", Static).content)


async def test_a_pane_with_no_console_recovery_behind_it_says_nothing_extra() -> None:
    """A pane in a bare terminal has no console to settle, and must not imply one."""
    app = ProjectsPane(_context())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert list(app._notifications) == []
        assert "Choose a project" in str(app.screen.query_one("#status", Static).content)


class _Settling:
    def __init__(self, report: RecoveryReport | None = None, error: Exception | None = None):
        self._report = report
        self._error = error
        self.panes: list[str | None] = []

    async def settle(self, resident_pane: str | None = None) -> RecoveryReport:
        self.panes.append(resident_pane)
        if self._error is not None:
            raise self._error
        assert self._report is not None
        return self._report


def test_the_composition_hands_the_recovery_report_over_instead_of_printing_it(capsys) -> None:
    """The channel itself: `settle`'s report is carried to the surface, not to stderr.

    Asserted against the executed composition rather than bootstrap's source text, the same
    way the console opener's wiring is — and against the *stream*, because printing here is
    the exact defect: Textual takes the alternate screen microseconds later and erases it.
    """
    from remote_agents.bootstrap import _console_notes

    report = _recovered(moved=("a",), blocked=("b",))
    composer = _Settling(report)
    carried = _console_notes(composer, "%7")
    captured = capsys.readouterr()
    assert carried is report
    assert composer.panes == ["%7"], "settle is asked about *this* process's own pane"
    assert "b" not in captured.err, "a blocked note printed here is erased before it is read"
    assert "b" not in captured.out


def test_a_recovery_that_raises_leaves_the_surface_to_start_anyway(capsys) -> None:
    """A console that cannot be settled is still a console (DEC-040).

    `settle` reads the pane arrangement *before* its own try block, so a tmux hiccup there
    escapes it — and uncaught, it reaches the composition's failure handler and exits instead
    of starting a degraded surface. The plan promised this guarantee and it was never built;
    a Tier-2 review found the gap.
    """
    from remote_agents.bootstrap import _console_notes

    composer = _Settling(error=RuntimeError("tmux went away mid-arrangement"))
    assert _console_notes(composer, "%7") is None
    captured = capsys.readouterr()
    assert "tmux went away" not in captured.err


async def test_the_projects_pane_keeps_the_two_flows_that_begin_with_a_project() -> None:
    """Add project and Resume both start by choosing one, and this is where that happens.

    Not "Sessions": that pane is beside this one, and once an agent is displayed this pane is
    not on screen at all.
    """
    app = ProjectsPane(_context())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        offered = set(app.screen.active_bindings)
        assert "ctrl+n" in offered, offered
        assert "ctrl+s" not in offered, offered


def _usage(opaque_id: str, count: int, days_ago: float) -> ProjectUsage:
    return ProjectUsage(opaque_id, count, datetime.now(UTC) - timedelta(days=days_ago))


async def test_the_projects_pane_opens_in_recency_order() -> None:
    """The DEC-012 gap this stage closes: the bot ranked, this surface drew the registry.

    The catalogue is built infra-then-dev-area and the owner has been in `opaque-shift` all
    week, so the first draw -- not the second, not the one after a refresh -- must lead with
    it.
    """
    launcher = _Launcher(usage=(_usage("opaque-tools", 5, 1), _usage("opaque-infra", 40, 400)))
    app = ProjectsPane(_context(sessions=launcher))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)

        assert [option.id for option in choices.options] == ["opaque-tools", "opaque-infra"]


async def test_a_host_that_reports_no_usage_draws_the_unranked_catalogue() -> None:
    """Not an empty one. An unranked list is every project the owner can still launch."""
    launcher = _Launcher()
    app = ProjectsPane(_context(sessions=launcher))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)

        assert [option.id for option in choices.options] == ["opaque-infra", "opaque-tools"]


async def test_the_order_is_computed_per_refresh_and_never_per_render() -> None:
    """DEC-012's mechanism, which this stage supersedes one *other* clause of.

    A ranking recomputed per render would reshuffle the list under the owner's fingers as
    they type in the filter -- and would read the store on every keystroke to do it.
    """
    launcher = _Launcher(usage=(_usage("opaque-tools", 5, 1), _usage("opaque-infra", 40, 400)))
    app = ProjectsPane(_context(sessions=launcher))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        reads_after_first_draw = launcher.usage_reads

        app.screen.render_projects("a")
        await pilot.pause()
        app.screen.render_projects("")
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)

        assert launcher.usage_reads == reads_after_first_draw
        assert [option.id for option in choices.options] == ["opaque-tools", "opaque-infra"]


async def test_a_refresh_recomputes_the_order() -> None:
    """The other half: a refresh is exactly where a new order is expected."""
    launcher = _Launcher(usage=(_usage("opaque-tools", 5, 1), _usage("opaque-infra", 40, 400)))
    app = ProjectsPane(_context(sessions=launcher))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        before = launcher.usage_reads

        await app.screen.refresh_contents()
        await pilot.pause()

        assert launcher.usage_reads == before + 1
        choices = app.screen.query_one("#choices", OptionList)
        assert [option.id for option in choices.options] == ["opaque-tools", "opaque-infra"]


class _UnreadableUsage(_Launcher):
    async def project_usage(self) -> tuple[ProjectUsage, ...]:
        raise RuntimeError("the store is unreachable")


async def test_a_store_that_cannot_report_usage_still_draws_the_list() -> None:
    """The degradation is asserted rather than incidental.

    `_ordered` swallows the failure and returns the catalogue as it came, so a host whose
    store went away renders an *unranked* list rather than an empty one or a traceback. The
    same answer a host with no launch history gets, which is the point: the owner can still
    launch every project on the list.
    """
    app = ProjectsPane(_context(sessions=_UnreadableUsage()))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)

        assert [option.id for option in choices.options] == ["opaque-infra", "opaque-tools"]


_ORDER_KEY = "ctrl+t"


def _drawn(status: Static) -> str:
    """What the one-line region actually renders, which is the only thing the owner reads."""
    return "".join(status.render_line(row).text for row in range(status.size.height))


async def test_one_key_switches_the_order_and_the_rows_follow(tmp_path: Path) -> None:
    launcher = _Launcher(usage=(_usage("opaque-tools", 5, 1), _usage("opaque-infra", 40, 400)))
    app = ProjectsPane(_context(sessions=launcher, preferences_path=tmp_path / "prefs.json"))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        assert [option.id for option in choices.options] == ["opaque-tools", "opaque-infra"]

        await pilot.press(_ORDER_KEY)
        await pilot.pause()

        # dev-area/opaque-shift before infra/remote-agents: area first, then name.
        assert [option.id for option in choices.options] == ["opaque-tools", "opaque-infra"]
        assert app.project_order == ALPHABETICAL


async def test_the_switch_reorders_without_re_reading_the_catalogue(tmp_path: Path) -> None:
    """The key changes the order, not the contents. A filesystem scan is a refresh's job."""
    reads = 0

    def _scan():
        nonlocal reads
        reads += 1
        return (_INFRA, _TOOLS)

    app = ProjectsPane(_context(refresh_catalogue=_scan, preferences_path=tmp_path / "prefs.json"))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        before = reads

        await pilot.press(_ORDER_KEY)
        await pilot.pause()

        assert reads == before


async def test_the_status_line_names_the_active_order_in_words(tmp_path: Path) -> None:
    """DEC-010: the words carry it, so the region takes no severity and no colour."""
    app = ProjectsPane(_context(preferences_path=tmp_path / "prefs.json"))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        status = app.screen.query_one("#status", Static)
        recency_sentence = _drawn(status)
        assert "recent" in recency_sentence.casefold()

        await pilot.press(_ORDER_KEY)
        await pilot.pause()

        alphabetical_sentence = _drawn(status)
        assert "alphabetical" in alphabetical_sentence.casefold()
        assert alphabetical_sentence != recency_sentence
        assert not status.has_class("-error") and not status.has_class("-warning")


async def test_the_filter_survives_the_switch(tmp_path: Path) -> None:
    """Reordering does not leave the position, so it has no business discarding the query.

    The same argument Ctrl+R was corrected by: a key that stays put must keep what is typed.
    """
    app = ProjectsPane(_context(preferences_path=tmp_path / "prefs.json"))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        entry = app.screen.query_one("#filter", Input)
        await pilot.click("#filter")
        await pilot.press(*"opaque-shift")
        await settle_filter(pilot)
        choices = app.screen.query_one("#choices", OptionList)
        assert [option.id for option in choices.options] == ["opaque-tools"]

        await pilot.press(_ORDER_KEY)
        await pilot.pause()

        assert entry.value == "opaque-shift"
        assert [option.id for option in choices.options] == ["opaque-tools"]


async def test_the_new_mode_is_written_once_per_press(tmp_path: Path) -> None:
    path = tmp_path / "prefs.json"
    app = ProjectsPane(_context(preferences_path=path))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert not path.exists(), "opening the list is not a choice worth recording"

        await pilot.press(_ORDER_KEY)
        await pilot.pause()
        assert read_project_order(path) == ALPHABETICAL

        await pilot.press(_ORDER_KEY)
        await pilot.pause()
        assert read_project_order(path) == RECENCY


async def test_the_remembered_order_is_the_one_the_surface_opens_in(tmp_path: Path) -> None:
    """The whole point of writing it: a restart lands where the owner left off."""
    path = tmp_path / "prefs.json"
    write_project_order(path, ALPHABETICAL)

    app = ProjectsPane(_context(preferences_path=path))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        assert app.project_order == ALPHABETICAL
        status = app.screen.query_one("#status", Static)
        assert "alphabetical" in _drawn(status).casefold()


async def test_a_host_that_wired_no_preferences_path_still_switches(tmp_path: Path) -> None:
    """It forgets between runs; it does not refuse the key."""
    app = ProjectsPane(_context())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        await pilot.press(_ORDER_KEY)
        await pilot.pause()

        assert app.project_order == ALPHABETICAL
        assert list(tmp_path.iterdir()) == []
