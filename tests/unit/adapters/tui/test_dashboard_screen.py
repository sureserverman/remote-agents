"""The resting position is a dashboard: projects left, sessions and feed on the right.

The dashboard subclasses the projects picker rather than replacing it, so every behavior
the resting position already had — the filter and its debounce, the catalogue refresh,
the back-path guarantee of a stack that can never empty — is inherited, not re-implemented.
What this file pins is the new shape: three panes exist, focus can reach both lists, each
pane declares its empty state (DEC-009), the app rests on the dashboard, and a pushed flow
still returns to it on Escape.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from backends import SessionUseCaseDouble, backend_for
from textual.widgets import OptionList
from tui_feedback import announcements

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.screens.dashboard import NO_LIMITS, DashboardScreen
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
from remote_agents.ports.agent_activity import ActivityKind
from remote_agents.ports.agent_usage import AgentLimits, UsageWindow

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")


class _Creator:
    def available_areas(self) -> tuple[str, ...]:
        return ("dev-area", "infra")

    def create(self, command: CreateProjectCommand) -> CreatedProject:
        identity = ProjectIdentity(area=command.area, name=command.name)
        return CreatedProject(identity, Path("/dev") / command.area / command.name)


class _Launcher(SessionUseCaseDouble):
    def __init__(self, records: tuple[SessionRecord, ...] = ()) -> None:
        self.records = records

    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records


def _record(slug: str = "existing") -> SessionRecord:
    return SessionRecord(
        _SESSION,
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity(slug, "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


def _context(records: tuple[SessionRecord, ...] = (), feed=None, limits=None) -> TuiContext:
    return TuiContext(
        backend=backend_for(
            sessions=_Launcher(records),  # type: ignore[arg-type]
            projects=_Creator(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_PROJECT,),
            catalogue=(_PROJECT,),
            activity_feed=feed,
            limits=limits,
        ),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


def test_the_resting_screen_is_the_dashboard() -> None:
    assert isinstance(RemoteAgentsTui(_context()).get_default_screen(), DashboardScreen)


async def test_three_panes_render_and_the_session_appears_in_its_pane() -> None:
    app = RemoteAgentsTui(_context((_record(),)))
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        projects = screen.query_one("#choices", OptionList)
        sessions = screen.query_one("#sessions-pane", OptionList)
        feed = screen.query_one("#feed-pane", OptionList)
        assert projects.option_count == 1
        assert projects.get_option_at_index(0).id == "opaque-existing"
        assert sessions.option_count == 1
        assert feed.display is True


async def test_the_sessions_pane_declares_its_empty_state() -> None:
    app = RemoteAgentsTui(_context(()))
    async with app.run_test() as pilot:
        await pilot.pause()
        sessions = app.screen.query_one("#sessions-pane", OptionList)
        assert sessions.option_count == 1
        option = sessions.get_option_at_index(0)
        assert option.disabled is True
        assert "No sessions" in str(option.prompt)


async def test_focus_can_reach_both_lists() -> None:
    app = RemoteAgentsTui(_context((_record(),)))
    async with app.run_test() as pilot:
        await pilot.pause()
        sessions = app.screen.query_one("#sessions-pane", OptionList)
        sessions.focus()
        await pilot.pause()
        assert app.focused is sessions
        projects = app.screen.query_one("#choices", OptionList)
        projects.focus()
        await pilot.pause()
        assert app.focused is projects


async def test_a_pushed_flow_returns_to_the_dashboard_on_escape() -> None:
    app = RemoteAgentsTui(_context())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        await app.push_screen(
            __import__(
                "remote_agents.adapters.tui.screens.launch", fromlist=["ProfilesScreen"]
            ).ProfilesScreen()
        )
        await pilot.pause()
        assert not isinstance(app.screen, DashboardScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)


async def test_open_session_from_the_pane_routes_through_the_one_seam() -> None:
    """Enter on a session row goes through `_open_or_leave`, so hosting decides what
    opening means; with the console capability wired the surface stays alive."""
    opened: list[str] = []

    async def opener(session_id: str) -> None:
        opened.append(session_id)

    from dataclasses import replace

    app = RemoteAgentsTui(replace(_context((_record(),)), open_in_console=opener))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.screen.choose(f"session:{_SESSION}")
        await pilot.pause()
        assert opened == [str(_SESSION)]
        assert app.is_running


async def test_open_session_detail_is_one_key_away() -> None:
    from remote_agents.adapters.tui.screens.sessions import SessionDetailScreen

    app = RemoteAgentsTui(_context((_record(),)))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.screen.query_one("#sessions-pane", OptionList)
        pane.highlighted = 0
        pane.focus()
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, SessionDetailScreen)


# The project a session row names on this surface ----------------------------------------------


async def test_the_sessions_region_names_the_project_and_keeps_its_option_id() -> None:
    """The dashboard's pane is the second render of the same record, and it must agree.

    Both halves are asserted together on purpose: the name is what the owner reads, and the
    option id is what `choose` and `action_session_detail` route on. A change that fixed the
    first and moved the second would look right on screen and strand every action behind it.
    """
    app = RemoteAgentsTui(_context((_record(slug="opaque-existing"),)))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.screen.query_one("#sessions-pane", OptionList)
        row = pane.get_option_at_index(0)
        assert "existing" in str(row.prompt)
        assert "opaque-existing" not in str(row.prompt)
        assert row.id == f"session:{_SESSION}"


# Enter on a feed row is routed, not mistaken for a project ------------------------------------


def _observation():
    from datetime import timedelta

    from remote_agents.ports.agent_activity import ActivityConfidence, AgentActivity

    return AgentActivity(
        str(_SESSION),
        ActivityKind.NEEDS_ANSWER,
        "May I push?",
        datetime.now(UTC) - timedelta(minutes=1),
        ActivityConfidence.REPORTED,
    )


async def test_a_feed_row_routes_by_its_prefix_and_never_reaches_the_project_branch() -> None:
    """The dashboard's `choose` is one method serving three panes, so a key it does not
    recognise falls through to the project half -- which answers by *announcing* that the
    project is unavailable. Before the `notification:` branch existed, Enter on a notification
    produced exactly that: a warning about a project, for a row that is not one.

    Asserted on the announcement rather than on a spy, because the announcement is the thing
    the owner would actually have seen.
    """

    async def feed():
        return (_observation(),)

    app = RemoteAgentsTui(_context((_record(),), feed=feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.screen.query_one("#feed-pane", OptionList)
        key = pane.get_option_at_index(0).id
        assert key.startswith("notification:")

        await app.screen.choose(key)
        await pilot.pause()

        assert "no longer available" not in " ".join(announcements(app)), (
            "a notification key fell through to the project branch"
        )
        assert app.screen.position == "DASHBOARD", "a feed row must not move the position"


async def test_choosing_a_project_row_is_unaffected_by_the_feed_branch() -> None:
    """The other half of the same seam: adding a prefix branch must not shadow the fallthrough
    every project row depends on."""

    async def feed():
        return (_observation(),)

    app = RemoteAgentsTui(_context((_record(),), feed=feed))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        # PROJECT_CHOOSER, not PROFILES: on this surface a project row asks "launch new or
        # reopen saved" first (DEC-033). The point of the assertion is that it still gets
        # somewhere, not which somewhere.
        assert app.screen.position == "PROJECT_CHOOSER", "a project row must still route"


# --- the limits pane ----------------------------------------------------------------------


def _limits_reader(*entries: AgentLimits):
    async def read() -> tuple[AgentLimits, ...]:
        return entries

    return read


async def test_the_limits_pane_sits_between_the_sessions_and_the_notifications() -> None:
    """Where the owner asked for it: "on the right, between the sessions and notifications".

    Order is asserted through the right column's own children rather than by querying each id,
    because the ask is about position and a test that only proved existence would pass with the
    pane anywhere on the screen.
    """
    app = RemoteAgentsTui(_context((_record(),)))
    async with app.run_test() as pilot:
        await pilot.pause()
        column = app.screen.query_one("#dashboard-right")
        ids = [child.id for child in column.children]

        assert ids == ["sessions-pane", "limits-pane", "feed-pane"]


async def test_the_limits_pane_declares_its_empty_state_before_any_read() -> None:
    """DEC-009, and seeded at compose time for the reason `#feed-pane` is.

    `_reload_limits` returns early when the capability is absent or the read raises, so a pane
    left blank would answer both of those with an empty box instead of the sentence the
    decision requires it to declare.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.screen.query_one("#limits-pane", OptionList)

        assert pane.option_count == 1
        option = pane.get_option_at_index(0)
        assert option.disabled is True
        assert "No agent limits" in str(option.prompt)


async def test_the_limits_pane_draws_one_row_per_answering_agent() -> None:
    app = RemoteAgentsTui(
        _context(
            limits=_limits_reader(
                AgentLimits(ProfileId("claude"), (UsageWindow("5h", 2.0),)),
                AgentLimits(ProfileId("codex"), (UsageWindow("week", 61.0),)),
            )
        )
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.screen.query_one("#limits-pane", OptionList)
        drawn = [str(pane.get_option_at_index(i).prompt) for i in range(pane.option_count)]

        assert drawn == ["claude: 5h 2%", "codex: week 61%"]


async def test_every_row_in_the_limits_pane_is_disabled() -> None:
    """It reports a fact and offers no action, so the cursor must not come to rest on it.

    The same reason `#feed-pane`'s empty row is disabled: a highlighted row on a pane with
    nothing to open answers Enter with silence, which reads as a broken key.
    """
    app = RemoteAgentsTui(
        _context(
            limits=_limits_reader(AgentLimits(ProfileId("codex"), (UsageWindow("week", 61.0),)))
        )
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.screen.query_one("#limits-pane", OptionList)

        assert all(pane.get_option_at_index(i).disabled for i in range(pane.option_count))


async def test_a_raising_limits_read_leaves_the_drawn_text_standing() -> None:
    """What is drawn is stale, not wrong, and a background read having a bad moment
    must not blank a pane the owner is reading."""

    async def exploding() -> tuple[AgentLimits, ...]:
        raise RuntimeError("the provider changed its layout under an upgrade")

    app = RemoteAgentsTui(_context(limits=exploding))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.screen.query_one("#limits-pane", OptionList)

        assert pane.option_count == 1
        assert "No agent limits" in str(pane.get_option_at_index(0).prompt)


async def test_a_host_that_wired_no_limits_reader_keeps_its_empty_sentence() -> None:
    app = RemoteAgentsTui(_context(limits=None))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.screen.query_one("#limits-pane", OptionList)

        assert pane.option_count == 1
        assert "No agent limits" in str(pane.get_option_at_index(0).prompt)


async def test_the_limits_pane_rides_the_tick_the_other_two_panes_ride() -> None:
    """One schedule, three panes -- so the right column cannot drift out of step with itself."""
    reads = []

    async def counting() -> tuple[AgentLimits, ...]:
        reads.append(1)
        return (AgentLimits(ProfileId("codex"), (UsageWindow("week", 61.0),)),)

    app = RemoteAgentsTui(_context(limits=counting))
    async with app.run_test() as pilot:
        await pilot.pause()
        first = len(reads)
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        await screen._reload_sessions_pane()

        # `== 1`, not `>= 1`: a double read at mount is a defect this would otherwise pass.
        assert first == 1
        assert len(reads) == first + 1


async def test_a_read_that_finds_nothing_withdraws_the_figures_it_last_drew() -> None:
    """A successful empty read is not a failed read, and the pane used to treat them alike.

    Claude's borrowed cache is fenced at thirty minutes, so half an hour of idleness is the
    routine way for a reader to go from answering to answering nothing. Returning early there
    pinned the last percentage on screen permanently -- countdown included, so `(resets in 3h)`
    still read 3h four hours later -- while the bot correctly dropped its block at the same
    instant. Two surfaces asserting different things about one account is the divergence sharing
    `limit_lines` exists to prevent.
    """
    answers = [
        (AgentLimits(ProfileId("claude"), (UsageWindow("5h", 2.0),)),),
        (AgentLimits(ProfileId("claude"), ()),),
    ]

    async def withdrawing() -> tuple[AgentLimits, ...]:
        return answers.pop(0) if answers else ()

    app = RemoteAgentsTui(_context(limits=withdrawing))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.screen.query_one("#limits-pane", OptionList)
        drawn = [str(pane.get_option_at_index(i).prompt) for i in range(pane.option_count)]
        assert drawn == ["claude: 5h 2%"]

        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        await screen._reload_limits()

        redrawn = [str(pane.get_option_at_index(i).prompt) for i in range(pane.option_count)]
        assert redrawn == [NO_LIMITS]


async def test_a_failed_read_still_leaves_the_last_figures_standing() -> None:
    """The other half of the same distinction: what is drawn is stale, not known to be wrong."""
    answers: list[tuple[AgentLimits, ...]] = [
        (AgentLimits(ProfileId("claude"), (UsageWindow("5h", 2.0),)),)
    ]

    async def then_failing() -> tuple[AgentLimits, ...]:
        if answers:
            return answers.pop(0)
        raise RuntimeError("the provider changed its layout under an upgrade")

    app = RemoteAgentsTui(_context(limits=then_failing))
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        await screen._reload_limits()

        pane = screen.query_one("#limits-pane", OptionList)
        drawn = [str(pane.get_option_at_index(i).prompt) for i in range(pane.option_count)]
        assert drawn == ["claude: 5h 2%"]


@pytest.mark.parametrize("width", [60, 73, 74, 75, 86, 87, 100, 120])
async def test_no_row_of_the_limits_pane_is_cut_off_at_any_width(width: int) -> None:
    """The pane must be tall enough for what it actually wraps to, not for a chopped estimate.

    `ceil(len(line) / width)` counts a character chop; the renderer word-wraps, which needs
    *more* rows whenever a word straddles the boundary. The widths here are the ones a sweep
    found losing a row with the content both providers really publish -- the reset clause is
    what makes the lines long enough to matter, and the snapshot fixture happens not to carry
    it. A lost row is unreachable rather than merely ugly: every option is disabled, so the
    highlight never moves and nothing scrolls to it.
    """
    entries = (
        AgentLimits(
            ProfileId("claude"),
            (
                UsageWindow("5h", 4.0, resets_at=datetime.now(UTC) + timedelta(hours=2)),
                UsageWindow("week", 49.0, resets_at=datetime.now(UTC) + timedelta(hours=2)),
            ),
            stale_source="status-line cache",
        ),
        AgentLimits(
            ProfileId("codex"),
            (UsageWindow("week", 61.0, resets_at=datetime.now(UTC) + timedelta(hours=2)),),
        ),
    )

    async def reader() -> tuple[AgentLimits, ...]:
        return entries

    app = RemoteAgentsTui(_context(limits=reader))
    # Tall enough that `max-height` is not what bounds the pane, so this measures the fit alone.
    async with app.run_test(size=(width, 60)) as pilot:
        await pilot.pause()
        await pilot.pause()
        pane = app.screen.query_one("#limits-pane", OptionList)

        assert pane.max_scroll_y == 0, (
            f"at {width} columns the pane scrolls by {pane.max_scroll_y} row(s) that no key "
            "can reach, because every option is disabled"
        )
