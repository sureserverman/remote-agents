"""Unit tests for the local terminal wizard, driven headlessly through Textual."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from backends import tui_context_for
from textual.widgets import Input, OptionList
from tui_feedback import announcements, breadcrumb
from tui_feedback import status as _status
from tui_filter import settle_filter
from tui_positions import position

from remote_agents.adapters.tui.app import (
    AttachRequest,
    RemoteAgentsTui,
    label_or_error,
)
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.model import _BACK, _CANCEL
from remote_agents.adapters.tui.screens import ProfilesScreen
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.errors import ProjectCreationError
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_admin import CreatedProject, CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import SessionId, SessionState
from remote_agents.domain.projects import ProjectIdentity

_EXISTING = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_OTHER = CatalogProject("opaque-other", "other-thing", "dev-area", "Unregistered")


@dataclass(slots=True)
class FakeRecord:
    # Mirrors SessionRecord's tenth field. A fake missing it duck-types the record
    # everywhere except the one branch DEC-020 added, which is the branch that offers a
    # destructive action.
    orphan_provenance = None

    session_id: SessionId
    state: SessionState


class FakeLauncher:
    """Accept one launch and report the state the test asked for."""

    def __init__(self, state: SessionState = SessionState.RUNNING) -> None:
        self.state = state
        self.commands: list[LaunchCommand] = []

    async def launch(self, command: LaunchCommand) -> FakeRecord:
        self.commands.append(command)
        return FakeRecord(SessionId.new(), self.state)


class FakeCreator:
    def __init__(
        self, areas: tuple[str, ...] = ("dev-area", "infra"), error: Exception | None = None
    ) -> None:
        self._areas = areas
        self.error = error
        self.commands: list[CreateProjectCommand] = []

    def available_areas(self) -> tuple[str, ...]:
        return self._areas

    def create(self, command: CreateProjectCommand) -> CreatedProject:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        identity = ProjectIdentity(area=command.area, name=command.name)
        return CreatedProject(identity, Path("/dev") / command.area / command.name)


def _context(**overrides: object) -> TuiContext:
    arguments: dict[str, object] = {
        "sessions": FakeLauncher(),
        "projects": FakeCreator(),
        "profiles": (
            ProfileAvailability("claude", True),
            ProfileAvailability("cursor-agent", False, "executable_missing"),
        ),
        "refresh_catalogue": lambda: (_EXISTING, _OTHER),
        "attach_argv": lambda session_id: (
            "tmux",
            "-L",
            "remote-agents",
            "attach-session",
            "-t",
            f"={session_id}",
        ),
        "catalogue": (_EXISTING, _OTHER),
    }
    arguments.update(overrides)
    return tui_context_for(**arguments)


def _rows(app: RemoteAgentsTui) -> list[str]:
    return [str(option.prompt) for option in app.screen.query_one("#choices", OptionList).options]


def _names(app: RemoteAgentsTui) -> list[str]:
    """The project each row names -- its first token -- on the two-column projects list.

    The redesign's row is the project's name padded out to a last-launch age against the right
    edge, so the whole string depends on the pane's width; the name is what a test is about.
    """
    return [row.split()[0] for row in _rows(app)]


async def _type_filter(pilot, text: str) -> None:
    """`/` to reach the filter (the keyboard rests on the rows), then the characters."""
    await pilot.press("slash")
    for character in text:
        await pilot.press(character)


def _keys(app: RemoteAgentsTui) -> list[str]:
    return [option.id for option in app.screen.query_one("#choices", OptionList).options]


async def _choose(app: RemoteAgentsTui, pilot, key: str) -> None:
    """Select a row on whatever screen is showing.

    The wizard positions are screens now, so "which handler acts on this key" is answered by
    what is on top of the stack rather than by a field the test would have to set. Pausing
    after each choice is what lets the pushed screen mount before the next assertion reads
    it — `app.screen` is the *active* screen, and `App.query_one` resolves against the stack
    bottom, so a read taken too early would report the previous position.
    """
    await app.screen.choose(key)
    await pilot.pause()


async def test_the_project_list_shows_registered_before_unregistered() -> None:
    app = RemoteAgentsTui(_context())

    async with app.run_test():
        names = _names(app)

    assert names == ["existing", "other-thing"]


async def test_typing_filters_the_project_list_one_character_at_a_time() -> None:
    """A refill must not steal the keyboard, or every character after the first is lost."""
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        await _type_filter(pilot, "other")
        await settle_filter(pilot)
        typed = app.screen.query_one("#filter").value
        names = _names(app)

    assert typed == "other"
    assert names == ["other-thing"]


async def test_the_agent_list_names_every_curated_profile_with_its_blocking_reason() -> None:
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        await _choose(app, pilot, "opaque-existing")
        await _choose(app, pilot, "launch")
        await pilot.pause()
        rows = _rows(app)

    # Back is part of the list now, not decoration: the agent list is the commit position, so
    # it carries the row DEC-007's mitigation rests the cursor on.
    assert rows == ["claude", "cursor-agent  (unavailable: executable_missing)", "Back"]


async def test_an_unavailable_agent_cannot_be_chosen() -> None:
    launcher = FakeLauncher()
    app = RemoteAgentsTui(_context(sessions=launcher))

    async with app.run_test() as pilot:
        await _choose(app, pilot, "opaque-existing")
        await _choose(app, pilot, "launch")
        await _choose(app, pilot, "cursor-agent")
        await pilot.pause()
        reported = " ".join(announcements(app, severity="warning"))
        status = _status(app)

    assert "cannot be launched" in reported
    assert "executable_missing" in reported
    assert status == ProfilesScreen.status, (
        "the refusal replaced the instruction the owner was following, which is the "
        "competition for one region this split exists to end"
    )
    # Read off the screen rather than quoted, because this test is about the region not being
    # overwritten and a literal here would fail on a rewording instead of on a regression.
    # `test_status_region.py` owns what the sentence must *say*.
    assert launcher.commands == []


async def test_choosing_an_agent_launches_with_no_step_in_between() -> None:
    """The launch flow is two positions, not three, and choosing the agent *is* the launch.

    It lost two steps, one per plan. The label step named a session before there was one to
    look at and could never change it afterwards — naming now lives on the session's own
    detail as Rename. The review step that replaced it as the commit position guarded nothing:
    once the label was gone it held two list selections, re-pickable in two keystrokes, and
    the resume flow has no equivalent position at all. DEC-033 says a step the other surface
    does not have is a step to remove.

    Asserted as *one issued launch*, not as the absence of a screen class: the claim is about
    what the owner walks through, and a flow that kept the step under another name would pass
    a test written against the import.
    """
    launcher = FakeLauncher()
    app = RemoteAgentsTui(_context(sessions=launcher))

    async with app.run_test() as pilot:
        await _choose(app, pilot, "opaque-existing")
        await _choose(app, pilot, "launch")
        assert position(app) == "PROFILES"
        await _choose(app, pilot, "claude")

    assert len(launcher.commands) == 1, (
        f"choosing an agent issued {len(launcher.commands)} launches; the agent list is the "
        f"commit position now, so exactly one was required"
    )
    assert str(launcher.commands[0].profile_id) == "claude"


async def test_the_agent_list_rests_on_the_row_that_launches_nothing() -> None:
    """The agent list became a commit position, so no repeated keypress may commit on it.

    DEC-007's mitigation, applied to the position that inherited the act. `ResumeConversations`
    went through this exact transformation when *its* confirmation was removed and its own
    comment records the hazard in the same words: two enters from the agent list — one to
    arrive, one still queued — would otherwise start a session and exec this terminal away.

    So the list grows a Back row and rests on it, and the first agent is one wrapping Down
    away. Asserted as the *drawn* cursor rather than as the row order, which is the
    distinction `test_resting_cursor.py` exists to draw: a row order with the cursor painted
    somewhere else proves nothing about what a stray enter does.
    """
    launcher = FakeLauncher()
    app = RemoteAgentsTui(_context(sessions=launcher))

    async with app.run_test(size=(100, 30)) as pilot:
        await _choose(app, pilot, "opaque-existing")
        await _choose(app, pilot, "launch")
        assert position(app) == "PROFILES"
        choices = app.screen.query_one("#choices", OptionList)
        keys = _keys(app)
        resting = choices.highlighted

        await pilot.press("enter")
        await pilot.pause()
        after_a_stray_enter = len(launcher.commands)

    assert keys[-1] == _BACK, f"the agent list offers no Back row to rest on: {keys}"
    assert resting == len(keys) - 1, (
        f"the cursor rests on row {resting} of {keys}, which is not the row that commits "
        f"nothing; a queued enter arriving here would launch an agent nobody chose"
    )
    assert after_a_stray_enter == 0, "an enter on the resting row issued a launch"


async def test_a_failed_launch_leaves_the_cursor_off_the_agent_it_tried() -> None:
    """A launch that failed must not be one queued keypress from being re-issued.

    The reasoning is inherited whole from the review position this replaces, because it was
    never about the review: `RemoteAgentsTui.launch` clears `_busy` in a `finally`, so the
    guard is open again the moment a failure returns, and the agent row the owner pressed is
    still under the cursor. The review re-rendered itself before reporting so the cursor left
    "Launch"; the agent list has no non-committing row to move to that is not also the way
    out, so it rests the cursor on **nothing** — `show_choices(highlight=None)`, the same
    mechanism `SessionsScreen._draw_listing` uses where a stray enter would stop an agent.

    Both halves of the failure are asserted alongside it. `failure.status` is what the owner
    may still need in a minute and it goes to the status line; `failure.explanation` is read
    once and goes to a toast. A regression that dropped either would still move the cursor.
    """
    launcher = FakeLauncher(state=SessionState.FAILED)
    app = RemoteAgentsTui(_context(sessions=launcher))

    async with app.run_test(size=(100, 30)) as pilot:
        await _choose(app, pilot, "opaque-existing")
        await _choose(app, pilot, "launch")
        await _choose(app, pilot, "claude")
        await pilot.pause()
        resting = app.screen.query_one("#choices", OptionList).highlighted
        step = position(app)
        status = _status(app)
        reported = " ".join(announcements(app, severity="error"))

    assert step == "PROFILES", f"a failed launch left the owner on {step}"
    assert resting is None, (
        f"the cursor rests on row {resting} after a failed launch; a second enter would "
        f"re-issue a launch nobody deliberately chose"
    )
    assert "did not become ready" in reported, reported
    assert "attach-session" in status, status


async def test_the_agent_list_names_the_project_before_any_launch() -> None:
    """The project, still before anything is created — carried entirely by the trail.

    This asserted three facts and the third was the label, which the status line held because
    the breadcrumb could not. With that step gone the trail carries what the owner chose, and
    what the status line owes them is what going through with it *does* — asserted by
    `test_status_region.py`, which owns that sentence.

    **It asserts one fact rather than two now, and that is the position moving rather than a
    claim being dropped.** The review stood *after* the agent choice, so the trail could name
    the agent behind it; the agent list stands *before* it, and the row under the cursor is
    what names the agent at this position. The launch that follows carries the profile id, and
    `test_choosing_an_agent_launches_with_no_step_in_between` asserts exactly that.
    """
    launcher = FakeLauncher()
    app = RemoteAgentsTui(_context(sessions=launcher))

    async with app.run_test() as pilot:
        await _choose(app, pilot, "opaque-existing")
        await _choose(app, pilot, "launch")
        await pilot.pause()
        trail = breadcrumb(app)
        keys = _keys(app)

    assert "infra/existing" in trail
    assert keys[:-1] == ["claude", "cursor-agent"], (
        f"the agent list must name every agent before any launch; it offered {keys}"
    )
    assert launcher.commands == [], "arriving at the agent list must not have launched anything"


async def test_back_from_the_agent_list_returns_to_the_project_choice_without_launching() -> None:
    """Back goes to the position it was reached from, one step at a time, and launches nothing.

    **This test has been narrowed twice, once per position removed, and the claim it makes is
    the same each time.** The hand-rolled chain this behaviour replaced had two shortcuts: Back
    at the review jumped straight past the label to the agent list, and Escape at the label
    jumped past the agent choice to the project list. On a real stack each was one level, and
    asserting the legs is what kept either from being reinstated. Removing the label removed
    the second shortcut's subject; removing the review has now removed the first's. What
    survives is the rule both were violations of — **one pop, one level** — asserted on the leg
    that still exists.

    It also absorbs what `test_cancel_at_review_returns_to_the_projects_without_launching`
    pinned. That test walked the `Cancel` row, which was the third of the triplet ask 5 names
    and left with the position; its surviving claim is that leaving this flow launches nothing,
    which is asserted here on the row that still exists. A `_CANCEL` that unwound to the
    project list from *within* the launch flow no longer has a screen to be pressed on.
    """
    launcher = FakeLauncher()
    app = RemoteAgentsTui(_context(sessions=launcher))

    async with app.run_test() as pilot:
        await _choose(app, pilot, "opaque-existing")
        await _choose(app, pilot, "launch")
        assert position(app) == "PROFILES", "expected the agent list before walking back from it"

        await _choose(app, pilot, _BACK)
        await pilot.pause()
        step = position(app)

    assert step == "PROJECT_CHOOSER", f"back from the agent list landed on {step}"
    assert launcher.commands == [], "walking back out of the flow launched something"


async def test_confirming_issues_one_launch_and_it_carries_no_label() -> None:
    launcher = FakeLauncher()
    app = RemoteAgentsTui(_context(sessions=launcher))

    async with app.run_test() as pilot:
        await _choose(app, pilot, "opaque-existing")
        await _choose(app, pilot, "launch")
        await _choose(app, pilot, "claude")

    assert len(launcher.commands) == 1
    command = launcher.commands[0]
    assert str(command.project_id) == "opaque-existing"
    assert str(command.profile_id) == "claude"
    # No label, and asserted rather than left unmentioned: this is the field the removed step
    # used to fill, and both surfaces now launch without one. A session is named afterwards,
    # from its own detail, which is the only moment the name can also be *changed*.
    assert command.label is None
    assert command.idempotency_key.startswith("tui-")


async def test_two_launches_never_reuse_an_idempotency_key() -> None:
    launcher = FakeLauncher()
    keys = []
    for _ in range(2):
        app = RemoteAgentsTui(_context(sessions=launcher))
        async with app.run_test() as pilot:
            await _choose(app, pilot, "opaque-existing")
            await _choose(app, pilot, "launch")
            await _choose(app, pilot, "claude")
            await _choose(app, pilot, "launch")
    keys = [command.idempotency_key for command in launcher.commands]

    assert len(set(keys)) == 2


async def test_a_failed_launch_reports_and_returns_to_the_agent_list_without_attaching() -> None:
    launcher = FakeLauncher(state=SessionState.FAILED)
    app = RemoteAgentsTui(_context(sessions=launcher))

    async with app.run_test() as pilot:
        await _choose(app, pilot, "opaque-existing")
        await _choose(app, pilot, "launch")
        await _choose(app, pilot, "claude")
        await pilot.pause()
        reported = " ".join(announcements(app, severity="error"))
        keys = _keys(app)

    assert "did not become ready" in reported
    # The rows are the agent list's own, unchanged by the failure — the owner can pick again
    # without leaving. Where the *cursor* sits after one is
    # `test_a_failed_launch_leaves_the_cursor_off_the_agent_it_tried`'s claim, not this one.
    assert keys[:-1] == ["claude", "cursor-agent"], keys
    assert app.return_value is None


async def test_the_area_list_comes_from_the_creation_service() -> None:
    app = RemoteAgentsTui(_context(projects=FakeCreator(areas=("dev-area", "infra"))))

    async with app.run_test() as pilot:
        await app.action_add_project()
        await pilot.pause()
        keys = _keys(app)

    assert keys == ["dev-area", "infra", _CANCEL]


async def test_no_eligible_area_is_reported_rather_than_shown_empty() -> None:
    app = RemoteAgentsTui(_context(projects=FakeCreator(areas=())))

    async with app.run_test() as pilot:
        await app.action_add_project()
        await pilot.pause()
        status = _status(app)

    assert "No area is available" in status


@pytest.mark.parametrize("name", ["New Thing", "has space", "UPPER", "../escape", ""])
async def test_a_new_project_name_outside_the_slug_rule_creates_nothing(name: str) -> None:
    creator = FakeCreator()
    app = RemoteAgentsTui(_context(projects=creator))

    async with app.run_test() as pilot:
        await app.action_add_project()
        await pilot.pause()
        await app.screen.choose("infra")
        await pilot.pause()
        app.screen.submit(name)
        await pilot.pause()

    assert creator.commands == []


async def test_a_created_project_is_selectable_without_leaving_the_app() -> None:
    creator = FakeCreator()
    created = CatalogProject("opaque-new", "brand-new", "infra", "Registered")
    app = RemoteAgentsTui(_context(projects=creator, refresh_catalogue=lambda: (created,)))

    async with app.run_test() as pilot:
        await app.action_add_project()
        await pilot.pause()
        await app.screen.choose("infra")
        await pilot.pause()
        app.screen.submit("brand-new")
        await pilot.pause()
        await app.screen.choose("create")
        await pilot.pause()
        names = _names(app)

    assert creator.commands == [CreateProjectCommand("infra", "brand-new")]
    assert names == ["brand-new"]


async def test_a_refused_creation_is_reported_and_leaves_the_catalogue_alone() -> None:
    creator = FakeCreator(error=ProjectCreationError("project directory already exists"))
    app = RemoteAgentsTui(_context(projects=creator))

    async with app.run_test() as pilot:
        await app.action_add_project()
        await pilot.pause()
        await app.screen.choose("infra")
        await pilot.pause()
        app.screen.submit("brand-new")
        await pilot.pause()
        await app.screen.choose("create")
        await pilot.pause()
        reported = " ".join(announcements(app, severity="error"))

    assert "Project not created" in reported
    assert "already exists" in reported


async def test_refresh_re_reads_a_project_another_process_created() -> None:
    later = CatalogProject("opaque-cli", "cli-made", "infra", "Registered")
    app = RemoteAgentsTui(_context(refresh_catalogue=lambda: (_EXISTING, later)))

    async with app.run_test() as pilot:
        await app.action_refresh()
        await pilot.pause()
        names = _names(app)

    assert "cli-made" in names


def test_label_normalisation_collapses_whitespace_and_bounds_length() -> None:
    assert label_or_error("  a   b  ", 40) == "a b"
    assert label_or_error("   ", 40) is None
    with pytest.raises(ValueError):
        label_or_error("x" * 41, 40)


def test_an_attach_request_carries_the_session_and_its_argument_vector() -> None:
    request = AttachRequest(
        "abc", ("tmux", "-L", "remote-agents", "attach-session", "-t", "=ra-abc")
    )

    assert request.session_id == "abc"
    assert "attach-session" in request.command
    assert request.argv[0] == "tmux"


async def test_the_keyboard_can_drive_a_launch_without_touching_a_private_method() -> None:
    """Private-method tests cannot see focus; only real keys prove the surface is usable."""
    launcher = FakeLauncher()
    app = RemoteAgentsTui(_context(sessions=launcher))

    async with app.run_test() as pilot:
        # The keyboard rests on the rows since the redesign, so one enter chooses the project.
        await pilot.press("enter")
        await pilot.pause()
        # The chooser sits between the project and the agents now; its cursor rests on
        # Launch, so one more enter walks through it.
        assert _keys(app)[:1] == ["launch"]
        await pilot.press("enter")
        await pilot.pause()
        assert _keys(app) == ["claude", "cursor-agent", _BACK]

        # **The agent list is the last position, and the launch is one Down and one enter from
        # here.** It rests on Back, so reaching an agent means moving the cursor on purpose —
        # the same shape the review had, on the screen that inherited the act. Down rather
        # than Up, and that is the assertion doing work: it proves the wrap this position's
        # own comment depends on, since Down from the last row reaches the first only because
        # `find_next_enabled` wraps.
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

    assert len(launcher.commands) == 1
    assert str(launcher.commands[0].project_id) == "opaque-existing"


async def test_every_choice_list_hands_the_keyboard_to_the_list() -> None:
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        choices = app.screen.query_one("#choices")
        assert choices.has_focus and choices.highlighted == 0, (
            "the project list opens with the keyboard on its rows, resting on the first"
        )

        await pilot.press("slash")
        await pilot.pause()
        assert app.screen.query_one("#filter").has_focus, "`/` hands the keyboard to the filter"
        await pilot.press("enter")
        await pilot.pause()
        assert choices.has_focus and choices.highlighted == 0, (
            "leaving the filter must hand the keyboard back to the rows, resting on the first"
        )

        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#choices").has_focus, "the chooser takes the keyboard"

        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#choices").has_focus, "the agent list takes the keyboard"

        # The walk used to end on the label entry taking the keyboard back, then on the
        # review. Both steps are gone and the agent list is the end of it -- so this presses
        # enter on the row the agent list rests on, which is Back, and the claim this test
        # makes is unchanged: every choice list hands the keyboard to its list, including the
        # one a pop returns to.
        assert position(app) == "PROFILES"

        await pilot.press("enter")
        await pilot.pause()
        assert position(app) == "PROJECT_CHOOSER", (
            "the agent list rests on Back, so an enter here is the way out of the flow"
        )
        assert app.screen.query_one("#choices").has_focus


async def test_the_add_project_binding_opens_the_area_list() -> None:
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()

        assert _keys(app)[:2] == ["dev-area", "infra"]
        assert app.screen.query_one("#choices").has_focus


async def test_the_refresh_binding_re_reads_the_catalogue() -> None:
    later = CatalogProject("opaque-cli", "cli-made", "infra", "Registered")
    app = RemoteAgentsTui(_context(refresh_catalogue=lambda: (later,)))

    async with app.run_test() as pilot:
        await pilot.press("ctrl+r")
        await pilot.pause()

        assert _names(app) == ["cli-made"]


async def test_typing_a_new_project_name_reviews_it_before_creating_anything() -> None:
    creator = FakeCreator()
    app = RemoteAgentsTui(_context(projects=creator))

    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#filter").has_focus

        app.screen.query_one("#filter").value = "typed-name"
        await pilot.press("enter")
        await pilot.pause()

        assert creator.commands == []
        assert _keys(app) == ["create", "\x00back", "\x00cancel"]
        assert "typed-name" in _status(app)

        await pilot.press("up")
        await pilot.press("enter")
        await pilot.pause()

    assert creator.commands == [CreateProjectCommand("dev-area", "typed-name")]


async def test_cancelling_the_new_project_review_creates_nothing() -> None:
    creator = FakeCreator()
    app = RemoteAgentsTui(_context(projects=creator))

    async with app.run_test() as pilot:
        await app.action_add_project()
        await pilot.pause()
        await app.screen.choose("infra")
        await pilot.pause()
        app.screen.submit("typed-name")
        await pilot.pause()
        await app.screen.choose(_CANCEL)
        await pilot.pause()

    assert creator.commands == []


async def test_back_out_of_the_add_project_flow_stops_at_every_position() -> None:
    """Back walks the add-project flow out one position at a time.

    **A deliberate navigation change, the same pair Task 2.1 removed from the launch wizard.**
    The hand-rolled chain sent Escape at the name entry straight to the project list, skipping
    the area choice, because the name entry was grouped with the launch label as a text
    position; and it
    sent Back at the review straight to the area list, skipping the name — so an owner who
    mistyped a project name could not correct it, only start the flow again. On a real stack
    each is one level.

    Asserted as the whole walk rather than as any single destination, so reinstating either
    shortcut fails here instead of passing on the endpoint alone.
    """
    creator = FakeCreator()
    app = RemoteAgentsTui(_context(projects=creator))

    async with app.run_test() as pilot:
        await app.action_add_project()
        await pilot.pause()
        assert _keys(app)[:2] == ["dev-area", "infra"], "expected the area list"

        await app.screen.choose("infra")
        await pilot.pause()
        assert app.screen.query_one("#filter").has_focus, "expected the name entry"

        app.screen.submit("typed-name")
        await pilot.pause()
        assert _keys(app) == ["create", _BACK, _CANCEL], "expected the new-project review"

        await app.screen.choose(_BACK)
        await pilot.pause()
        assert app.screen.query_one("#filter").has_focus, (
            "back from the review must restore the name entry, not skip past it"
        )

        await app.action_back()
        await pilot.pause()
        assert _keys(app)[:2] == ["dev-area", "infra"], (
            "escape from the name entry must return to the area list, not to the projects"
        )

        await app.action_back()
        await pilot.pause()
        assert _names(app) == ["existing", "other-thing"]

    assert creator.commands == [], "walking back out must create nothing"


async def test_returning_to_the_project_list_clears_the_filter_and_rests_on_the_rows() -> None:
    """Backing out of any flow lands on a clean list with the keyboard on the rows.

    The sixth of this stage's navigation changes, and the only one that was a *regression*
    rather than a deliberate simplification. The chain this replaces reached the project list
    through a method that cleared the filter, so every back path landed on a fresh list. A
    bare pop returned the owner to a filtered list -- narrowed by a query they had finished
    with, with no sign of it.

    Since the redesign the keyboard rests on the rows and `/` reaches the filter, so what a
    return has to restore is the *unfiltered* list with its cursor on the first row.

    Driven through a real flow and real keys rather than by calling the reveal hook, because
    the failure was about *focus*, and a private-method test cannot see focus.
    """
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        await _type_filter(pilot, "other")
        await settle_filter(pilot)
        assert _names(app) == ["other-thing"], "expected a filtered list"

        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#choices").has_focus, "expected the keyboard on the rows"

        await _choose(app, pilot, "opaque-other")
        await _choose(app, pilot, "launch")
        assert _rows(app) == ["claude", "cursor-agent  (unavailable: executable_missing)", "Back"]

        # Two positions between the rows and home now: the agent list and the chooser.
        await app.action_back()
        await pilot.pause()
        await app.action_back()
        await pilot.pause()

        entry = app.screen.query_one("#filter")
        choices = app.screen.query_one("#choices", OptionList)
        assert entry.value == "", "the filter kept its text, so the list is still filtered"
        assert choices.has_focus and choices.highlighted == 0, (
            "the keyboard must rest on the rows with the cursor on the first"
        )
        assert _names(app) == ["existing", "other-thing"]


async def test_an_area_the_identity_rule_rejects_is_never_offered() -> None:
    app = RemoteAgentsTui(_context(projects=FakeCreator(areas=("infra", "Not_A_Slug", "web"))))

    async with app.run_test() as pilot:
        await app.action_add_project()
        await pilot.pause()
        keys = _keys(app)

    assert keys[:2] == ["infra", "web"]


async def test_a_launch_failure_outside_the_error_contract_does_not_kill_the_app() -> None:
    class Exploding(FakeLauncher):
        async def launch(self, command: LaunchCommand) -> FakeRecord:
            raise RuntimeError("the terminal port broke its contract")

    app = RemoteAgentsTui(_context(sessions=Exploding()))

    async with app.run_test() as pilot:
        await _choose(app, pilot, "opaque-existing")
        await _choose(app, pilot, "launch")
        await _choose(app, pilot, "claude")
        await pilot.pause()
        reported = " ".join(announcements(app, severity="error"))

    assert "was not started" in reported
    assert app.return_value is None


async def test_a_failed_launch_still_names_a_way_to_reach_its_pane() -> None:
    """A launch that never reported ready may still have left a pane running."""
    app = RemoteAgentsTui(_context(sessions=FakeLauncher(state=SessionState.FAILED)))

    async with app.run_test() as pilot:
        await _choose(app, pilot, "opaque-existing")
        await _choose(app, pilot, "launch")
        await _choose(app, pilot, "claude")
        await pilot.pause()
        status = _status(app)
        reported = " ".join(announcements(app, severity="error"))

    # The command the owner has to copy stays on screen; the reason they are being handed it
    # is said once. Both are asserted, because keeping only the toast would leave the command
    # to expire out from under them and keeping only the status would never say why.
    assert "attach-session" in status
    assert "did not become ready" in reported
    assert app.return_value is None


async def test_four_characters_in_quick_succession_search_the_catalogue_once(monkeypatch) -> None:
    """The filter waits for the typing to stop instead of re-searching per keystroke.

    Counted at `search_catalogue` rather than at the render, because the render is cheap and
    the search is the part that walks the whole catalogue. Before the debounce this was four
    searches and four full row rebuilds for one four-character word, three of them discarded
    before the owner could read them.

    **"Quick succession" is expressed by handing the screen four changes with no await between
    them, not by pressing four keys and hoping the machine is fast.** The first version of this
    test did the latter and asserted that nothing had been searched yet; it passed alone and
    failed inside the full suite, because four `pilot.press` calls on a loaded machine can take
    longer than the 120ms they are supposed to fit inside. An assertion about the debounce that
    is really an assertion about the host's spare capacity is worth less than no assertion.
    """
    import remote_agents.adapters.tui.screens.launch as launch

    calls: list[str] = []
    real = launch.search_catalogue

    def counting(catalogue, query):
        calls.append(query)
        return real(catalogue, query)

    monkeypatch.setattr(launch, "search_catalogue", counting)
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        entry = app.screen.query_one("#filter", Input)
        for typed in ("o", "ot", "oth", "othe"):
            entry.value = typed
            app.screen.on_input_changed(Input.Changed(entry, typed))
        await settle_filter(pilot)
        searched = list(calls)
        names = _names(app)

    assert searched == ["othe"], f"expected one search for the settled query, got {searched}"
    assert names == ["other-thing"]


async def test_typing_with_real_keys_searches_fewer_times_than_it_has_characters(
    monkeypatch,
) -> None:
    """The same claim through the real key path, stated so a slow machine cannot break it.

    The deterministic case above proves the debounce collapses a burst; this one proves the
    burst actually reaches it through `Input.Changed` from real keystrokes. It asserts an
    inequality rather than a count, because how many 120ms windows five keypresses fall across
    is a fact about the machine — but "fewer searches than characters" is false for the
    per-keystroke behaviour this replaced no matter how slow the host is.
    """
    import remote_agents.adapters.tui.screens.launch as launch

    calls: list[str] = []
    real = launch.search_catalogue

    def counting(catalogue, query):
        calls.append(query)
        return real(catalogue, query)

    monkeypatch.setattr(launch, "search_catalogue", counting)
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        await _type_filter(pilot, "other")
        await settle_filter(pilot)
        searched = list(calls)
        names = _names(app)

    assert len(searched) < 5, f"one search per keystroke survived: {searched}"
    assert searched[-1] == "other", f"the settled query was not the last one typed: {searched}"
    assert names == ["other-thing"]


async def test_down_arrow_moves_from_the_filter_into_the_results() -> None:
    """Enter was the only way out of the filter; the key an owner reaches for is down."""
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        await pilot.press("slash")
        await pilot.pause()
        assert app.screen.query_one("#filter").has_focus

        await pilot.press("down")
        await pilot.pause()
        choices = app.screen.query_one("#choices", OptionList)
        focused_rows = choices.has_focus
        highlighted = choices.highlighted

    assert focused_rows, "down-arrow left the keyboard in the filter"
    assert highlighted == 0, "down-arrow entered the rows without resting on the first one"


async def test_down_arrow_enters_the_filtered_rows_and_not_the_stale_ones() -> None:
    """The debounce's own hazard: leaving the filter before the scheduled search has run.

    Typed and left inside the debounce window, so the pending search has not fired when the
    key arrives. If down-arrow simply moved focus, the cursor would rest on the first row of
    the *unfiltered* catalogue — a different project from the one the owner narrowed to.
    """
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        await _type_filter(pilot, "other")
        await pilot.press("down")
        await pilot.pause()
        names = _names(app)
        choices = app.screen.query_one("#choices", OptionList)
        resting = str(choices.get_option_at_index(choices.highlighted).prompt).split()[0]

    assert names == ["other-thing"], "the pending filter was not applied"
    assert resting == "other-thing"


async def test_leaving_the_filter_with_enter_also_applies_a_pending_search() -> None:
    """Enter had the same hazard as down, and gets the same flush."""
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        await _type_filter(pilot, "other")
        await pilot.press("enter")
        await pilot.pause()
        names = _names(app)
        focused_rows = app.screen.query_one("#choices").has_focus

    assert names == ["other-thing"]
    assert focused_rows


async def test_an_invalid_project_name_is_rejected_while_it_is_being_typed() -> None:
    """`ProjectIdentity`'s own words, at the keystroke rather than at the submit."""
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        await _choose(app, pilot, "infra")
        entry = app.screen.query_one("#filter", Input)
        assert entry.has_focus, "expected the name entry"

        entry.value = "Not A Slug"
        await pilot.pause()

        rejected = announcements(app, severity="warning")
        invalid = entry.has_class("-invalid")

    assert invalid, "the entry did not mark itself invalid"
    assert any("lowercase letters, digits" in message for message in rejected), rejected


async def test_the_typed_time_validators_delegate_rather_than_restate_the_rules() -> None:
    """Both validators must produce the message the shared rule raises, character for character.

    This is what stops a validator drifting into a second copy of a rule: it does not compare
    against a string written here, it compares against what the shared function itself says.
    """
    from remote_agents.adapters.tui.screens.validation import (
        LabelWithinBound,
        NameIsAProjectIdentity,
    )

    try:
        label_or_error("x" * 500, 40)
    except ValueError as error:
        expected_label = str(error)
    else:  # pragma: no cover - the bound is what this test is about
        raise AssertionError("label_or_error accepted a 500-character label under a bound of 40")

    try:
        ProjectIdentity(area="infra", name="Not A Slug")
    except ValueError as error:
        expected_name = str(error)
    else:  # pragma: no cover
        raise AssertionError("ProjectIdentity accepted a name that is not a slug")

    label_result = LabelWithinBound(40).validate("x" * 500)
    name_result = NameIsAProjectIdentity("infra").validate("Not A Slug")

    assert label_result.failure_descriptions == [expected_label]
    assert name_result.failure_descriptions == [expected_name]


async def test_the_project_filter_is_not_validated() -> None:
    """A query matching nothing is a thing the owner typed, not a value being refused."""
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        entry = app.screen.query_one("#filter", Input)
        entry.value = "no-such-project"
        await settle_filter(pilot)

        invalid = entry.has_class("-invalid")
        rejected = announcements(app, severity="warning")
        validators = list(entry.validators)

    assert validators == []
    assert not invalid
    assert rejected == []


async def test_the_name_entry_opens_without_refusing_the_value_it_has_not_been_given() -> None:
    """`valid_empty=False` must reject an empty name on submit, not on arrival.

    A review found this property holding by coincidence: `text_entry` used to assign the
    value before the validators, and no spurious rejection fired only because Textual's own
    default for `valid_empty` already matched what this screen wanted, so the reactive
    watcher never ran. Both orderings are now arranged rather than inherited, and this is
    what would notice if they slipped — the sibling label case has asserted the same shape
    all along, which is how the asymmetry was spotted.
    """
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        await _choose(app, pilot, "infra")
        entry = app.screen.query_one("#filter", Input)

        # Every one of these is read *inside* the running app, deliberately. Reading them
        # after the block asks a torn-down widget: shutdown blurs the entry, `validate_on`
        # includes "blur", and an empty value under `valid_empty=False` is invalid — so an
        # assertion taken outside reports a rejection that never happened while the owner was
        # looking at the screen. The first draft of this test did exactly that and failed.
        focused = entry.has_focus
        rejected = announcements(app, severity="warning")
        invalid = entry.has_class("-invalid")

    assert focused, "expected the name entry"
    assert rejected == [], f"the entry refused a value before one was typed: {rejected}"
    assert not invalid, "the entry opened already marked invalid"


async def test_the_name_entry_still_refuses_an_empty_name_when_it_is_submitted() -> None:
    """The other half of the same rule: arrival is silent, submit is not."""
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        await _choose(app, pilot, "infra")
        before = app.screen.position

        await pilot.press("enter")
        await pilot.pause()
        rejected = announcements(app, severity="warning")
        after = app.screen.position

    assert before == "NAME"
    assert after == "NAME", "an empty project name was accepted"
    assert any("lowercase letters, digits" in message for message in rejected), rejected
