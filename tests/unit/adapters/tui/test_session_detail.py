"""Selecting a session opens a detail view that explains what it is."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from backends import backend_for
from textual.widgets import OptionList
from tui_feedback import announcements, breadcrumb
from tui_feedback import status as _status
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_actions import explain_state
from remote_agents.application.session_views import with_project_names
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import RemoteControlState

_EXISTING = CatalogProject("opaque-existing", "existing", "infra", "Registered")


def _record(
    state: SessionState = SessionState.RUNNING,
    *,
    slug: str = "opaque-existing",
    custom_label: str | None = None,
) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity(slug, "claude", "regular", 1, custom_label),
        state,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _Listing:
    records: tuple[SessionRecord, ...] = ()
    #: How many times the store has been read. A command that is refused before it starts
    #: never reaches `current_record`, so this is how a test tells "refused" from "issued
    #: and failed" without reaching into the surface's internals.
    reads: int = 0

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        self.reads += 1
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        return None


def _context(launcher: _Listing) -> TuiContext:
    return TuiContext(
        backend=backend_for(
            sessions=launcher,  # type: ignore[arg-type]
            projects=object(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_EXISTING,),
            catalogue=(_EXISTING,),
        ),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


# ENDED is deliberately unreachable in detail: it is filtered from the listing, so there is
# no row to select. Its explanation is still pinned, by tests/unit/application/
# test_state_explanations.py, which enumerates all 7 members.
_REACHABLE = [state for state in SessionState if state is not SessionState.ENDED]


@pytest.mark.parametrize("state", _REACHABLE)
async def test_detail_explains_every_reachable_lifecycle_state(state: SessionState) -> None:
    """Enumerated over the enum, so an unclassified state fails here rather than shipping."""
    record = _record(state)
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        status = _status(app)

    assert explain_state(state, None) in status


async def test_every_state_is_either_reachable_in_detail_or_deliberately_filtered() -> None:
    """Guards the exclusion above from silently growing to hide an unhandled state."""
    assert set(_REACHABLE) | {SessionState.ENDED} == set(SessionState)


async def test_an_ended_session_has_no_detail_to_open() -> None:
    """Filtered from the list, so selecting it can only mean it ended a moment ago."""
    record = _record(SessionState.ENDED)
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        status = _status(app)

    assert "no longer available" in status.casefold()


async def test_detail_names_the_session_and_its_state() -> None:
    """Both halves are still said; the status split decided *where*.

    The session's name is the header's breadcrumb — it is true of the whole position — and
    the state is the status line, which is what changes underneath it. Asserting both here
    rather than dropping one is the point: a split that quietly stopped naming the session
    would pass a test that had been narrowed to the state.
    """
    record = _record(SessionState.PRESERVED)
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        status = _status(app)
        trail = breadcrumb(app)

    (named,) = with_project_names((record,), (_EXISTING,))
    assert named.display.rendered in trail
    assert record.display.project_slug not in trail
    assert "preserved" in status


async def test_escape_returns_from_detail_to_the_list() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        assert position(app) == "SESSION_DETAIL"
        await app.action_back()
        await pilot.pause()
        step = position(app)

    assert step == "SESSIONS"


async def test_a_session_that_vanished_between_list_and_detail_does_not_raise() -> None:
    """The store is shared; a session can be stopped elsewhere while this list is open."""
    listed = _record()
    launcher = _Listing((listed,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        launcher.records = ()
        await app.show_detail(str(listed.session_id))
        await pilot.pause()
        status = _status(app)

    assert "no longer available" in status.casefold()


async def test_selecting_a_row_opens_its_detail() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        step = position(app)
        trail = breadcrumb(app)

    assert step == "SESSION_DETAIL"
    (named,) = with_project_names((record,), (_EXISTING,))
    assert named.display.rendered in trail
    assert record.display.project_slug not in trail


# The project the breadcrumb names -------------------------------------------------------------


async def test_the_breadcrumb_names_the_project_rather_than_the_catalogue_id() -> None:
    """The detail is reached from a row that now reads the project's name, so a detail still
    showing the hex prefix would rename the session on the way in -- the owner would arrive
    somewhere that looks like a different session from the one they chose."""
    record = _record(slug="opaque-existing")
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        trail = breadcrumb(app)

    assert "existing" in trail
    assert "opaque-existing" not in trail


async def test_a_renamed_session_keeps_its_label_after_the_named_project() -> None:
    """`SessionDisplayIdentity.rendered` puts the custom label last, after the generated
    part. Naming the project rewrites one field *inside* the generated part, so the label
    must survive it -- a rename the owner performed is the one part of this string they
    chose, and losing it to a cosmetic fix would be the worse trade."""
    record = _record(slug="opaque-existing", custom_label="nightly sweep")
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        trail = breadcrumb(app)

    assert "existing" in trail
    assert "opaque-existing" not in trail
    assert trail.index("nightly sweep") > trail.index("existing")


# An opening action, performed through the chain the detail already has ------------------------


async def test_a_detail_opened_with_force_shows_the_same_confirmation_as_the_row() -> None:
    """The whole design of Stage 4 in one assertion.

    A key on the sessions pane must not grow a second confirmation chain: `confirm_force`
    holds `holding_the_guard()` across a store re-read *and* the whole modal, re-checks the
    policy before asking, and refreshes on abort. A second copy of that would be the single
    highest-risk thing this plan could do, so the pane's keys push this screen with an opening
    action and the detail performs it through the branch it already had.
    """
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id), opening_action="force")
        await pilot.pause()
        assert position(app) == "FORCE_MODAL", (
            "an opening force did not reach the confirmation the row reaches"
        )
        # The modal's resting cursor is the abort (DEC-007's first mitigation), and it is
        # asserted here rather than assumed because this is a *new* way to arrive at it.
        choices = app.screen.query_one("#choices", OptionList)
        assert choices.highlighted == 0
        resting = str(choices.get_option_at_index(0).id)
        assert "cancel" in resting, resting
        # Answered before leaving, and this is a hazard worth naming for anyone adding a
        # case here: while a modal is open, `ask_to_confirm`'s worker is awaiting, so *any*
        # early exit from this block -- a failed assertion included -- hangs `run_test`
        # teardown instead of reporting. A wrong assertion here therefore looks exactly like
        # a deadlock in the production code, which cost real time to tell apart.
        await pilot.press("escape")
        await pilot.pause()


async def test_aborting_an_opened_confirmation_leaves_a_refreshed_detail() -> None:
    """Not the list. The owner asked to look at this session; declining the question they were
    asked about it must not also take away the thing they were looking at."""
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id), opening_action="force")
        await pilot.pause()
        assert position(app) == "FORCE_MODAL"

        await pilot.press("escape")
        await pilot.pause()
        assert position(app) == "SESSION_DETAIL", "the abort left the detail"


async def test_an_opening_action_the_policy_refuses_says_so_and_acts_on_nothing() -> None:
    """STARTING offers no stop at all -- the pane may not exist yet and the domain has no
    transition from it. The refusal is the *policy's* sentence, produced by the same re-check
    `confirm_force` runs for a pressed row, not a second opinion held by the caller."""
    record = _record(SessionState.STARTING)
    listing = _Listing((record,))
    app = RemoteAgentsTui(_context(listing))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id), opening_action="force")
        await pilot.pause()
        said = " ".join(announcements(app))
        assert position(app) == "SESSION_DETAIL", "a refused action still moved the owner"
        assert "Force stop" in said and "no longer available" in said, said


async def test_the_opening_action_does_not_fire_again_on_a_back_path() -> None:
    """`populate` runs once per mount and `on_reveal` runs on every return, so an action
    consumed by the wrong one would re-ask a destructive question every time the owner came
    back from Inspect or from an abort. Cleared before it is dispatched, so even a raising
    branch cannot leave it armed."""
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id), opening_action="force")
        await pilot.pause()
        assert position(app) == "FORCE_MODAL"
        await pilot.press("escape")
        await pilot.pause()
        assert position(app) == "SESSION_DETAIL"

        # Returning to the screen must not re-arm the question.
        await app.screen.on_reveal()
        await pilot.pause()
        assert position(app) == "SESSION_DETAIL", "coming back re-asked the confirmation"


async def test_a_detail_opened_with_no_action_behaves_exactly_as_before() -> None:
    """The argument is optional and its absence is the existing path, unchanged."""
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        assert position(app) == "SESSION_DETAIL"
        choices = app.screen.query_one("#choices", OptionList)
        keys = [choices.get_option_at_index(i).id for i in range(choices.option_count)]
        assert "force" in keys, "the detail lost its own rows"


async def test_a_key_on_a_detail_already_showing_still_performs_its_action() -> None:
    """The redraw branch of `show_detail`, which does not re-mount and so never reaches
    `populate`. Every other case here exercises the push path; this is why the redraw branch
    posts its own `OpeningAction` rather than relying on the constructor argument.

    Driven with `attach` rather than `force`, and the reason is the mechanism working as
    designed. A confirmation is asked from a screen handler *precisely so the handler holds
    the screen's message pump while suspended* -- DEC-025's whole protection, and why
    `OpeningAction` is a posted message rather than a `call_after_refresh`. In a test the
    consequence is that `pilot.pause()` cannot return while such a handler is suspended,
    because it is waiting on a pump that is deliberately blocked. The modal path is covered on
    the push path above, where the mount ordering lets the pilot through. What needs proving
    here is that the redraw branch dispatches at all, and a non-suspending action proves that
    without deadlocking the harness.
    """
    record = _record()
    listing = _Listing((record,))
    app = RemoteAgentsTui(_context(listing))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        detail = app.screen
        assert position(app) == "SESSION_DETAIL"
        before = listing.reads

        await app.show_detail(str(record.session_id), opening_action="attach")
        await pilot.pause()

        # `show_attach` re-reads the record through `current_record`, so the read count moving
        # is the evidence the action ran -- and it is the same evidence the busy-refusal test
        # uses for the opposite conclusion, which keeps the two readings comparable.
        assert listing.reads > before, "the redraw path never performed the action"
        assert app.screen is detail, "the redraw path pushed a second detail screen"
        assert position(app) == "SESSION_DETAIL"


async def test_an_opening_action_is_refused_while_a_command_is_in_flight() -> None:
    """The guard a pressed row already had, on the path that does not go through a row.

    `ChoiceScreen.on_option_list_option_selected` is the one place that drops a selection
    while the surface is busy -- and `RemoteAgentsTui.set_remote_control`'s own docstring
    names the consequence in advance: it has no busy check of its own precisely *because* the
    handler refuses first, and "a second caller reaching this directly would not be refused
    here, which is the thing to check before adding one."

    An opening action is exactly such a second caller. Without this, a key pressed while a
    stop was still in flight could start a second mutating command against the same session,
    and whichever finished first would clear `busy` while the other was still running.
    """
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        assert position(app) == "SESSION_DETAIL"

        app._busy = True
        try:
            await app.show_detail(str(record.session_id), opening_action="force")
            await pilot.pause()
            assert position(app) == "SESSION_DETAIL", (
                "an opening action opened a confirmation while a command was in flight"
            )
        finally:
            app._busy = False


async def test_set_remote_control_refuses_a_second_concurrent_command_itself() -> None:
    """The asymmetry `set_remote_control`'s docstring described, closed rather than described.

    It had no busy check because `on_option_list_option_selected` refused first -- true while
    that handler was the only route. Stage 4 adds a second, and the fix for *that* was to
    repeat the check in `dispatch_opening`. This makes it structural instead: `stop` already
    opens with the same guard, `set_remote_control` is called outside the confirmation's own
    `holding_the_guard()` exactly as `stop` is, and a rule enforced where the command is
    issued cannot be forgotten by a third caller the way a rule enforced at each entry can.

    Asserted on whether the store is read at all: a refused command never reaches
    `current_record`, so the listing's read count is the signal that nothing was issued. An
    earlier version of this test spied on a `backend.remote_control` attribute that is not on
    the path at all, and passed for that reason rather than for the right one.
    """
    record = _record()
    listing = _Listing((record,))
    app = RemoteAgentsTui(_context(listing))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        before = listing.reads

        app._busy = True
        try:
            await app.set_remote_control(
                str(record.session_id), RemoteControlState.ACTIVE, app.screen
            )
        finally:
            app._busy = False

        assert listing.reads == before, (
            "a Remote Control change read the store while another command was in flight"
        )
