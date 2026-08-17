"""The footer lists the keys that do something here, and nothing else.

Six app-level bindings were shown on all sixteen positions regardless of whether they applied.
Three of them already had early returns that made them inert in places — escape at the resting
position, Refresh where there is nothing to re-read, Resume on a host that wired no
conversation service — so the footer was advertising keys that did nothing, on exactly the
hosts and positions where the owner had least reason to guess why.

**These tests assert the footer against the actions, not against a copy of the rules.** Each
case drives the real screen and asks Textual what it would show; the expectation is derived
from the same condition the action itself checks. A test that restated the rule would pass for
a `check_action` that had drifted from the action it governs, which is the one failure this
whole approach exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from textual.screen import Screen
from textual.widgets import OptionList
from tui_filter import settle_filter

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.screens import ALL_SCREENS
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProfileResumeCapability,
    ResolvedConversation,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_SESSION_ID = SessionId.new()
_REFERENCE = ConversationReference("c-" + "0" * 14 + "01")


def _record() -> SessionRecord:
    return SessionRecord(
        _SESSION_ID,
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


def _summary() -> ConversationSummary:
    return ConversationSummary(
        _REFERENCE,
        ProfileId("claude"),
        ProjectId("opaque-existing"),
        ConversationState.RESUMABLE,
        datetime.now(UTC),
        description="a saved conversation",
    )


@dataclass(slots=True)
class _Launcher:
    record: SessionRecord = field(default_factory=_record)

    async def refresh_readiness(self):
        return (self.record,)

    async def list_sessions(self):
        return (self.record,)

    async def copy_attach(self, _session_id):
        return None


class _Creator:
    def available_areas(self):
        return ("dev-area", "infra")


class _Conversations:
    async def catalogue(self, query):
        return ConversationCataloguePage((_summary(),), query.page, 1)

    async def resolve_for_resume(self, _reference):
        return ResolvedConversation(_summary(), None)  # type: ignore[arg-type]

    async def capabilities(self):
        return (
            ProfileResumeCapability(
                ProfileId("claude"), catalogue_available=True, selected_resume_available=True
            ),
        )


def _context(*, conversations: bool = True) -> TuiContext:
    return TuiContext(
        launcher=_Launcher(),  # type: ignore[arg-type]
        creator=_Creator(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_PROJECT,),
        capture=lambda _session_id: _captured(),
        conversations=_Conversations() if conversations else None,  # type: ignore[arg-type]
    )


async def _captured() -> str:
    return "some output"


def _footer_keys(app: RemoteAgentsTui) -> set[str]:
    """The keys the footer would actually draw for the position on screen.

    `active_bindings` is what `Footer` renders from, and it is already filtered by
    `check_action` — a `None` answer drops the entry entirely. Reading it is therefore asking
    the framework what the owner sees, rather than asking our own predicate what it thinks.
    """
    return set(app.screen.active_bindings)


# Reached by pushing an instance directly, the same arrangement the back-path suite uses and
# for the same reason: a screen that cannot be built without the state it renders is the point.
def _arrangements():
    from remote_agents.adapters.tui.screens import (
        AreasScreen,
        ForceConfirmModal,
        InspectScreen,
        NameScreen,
        ProfilesScreen,
        ProjectReviewScreen,
        ProjectsScreen,
        RemoteControlConfirmModal,
        RenameScreen,
        ResumeConfirmScreen,
        ResumeConversationsScreen,
        ResumeProfilesScreen,
        ResumeProjectsScreen,
        ReviewScreen,
        SessionDetailScreen,
        SessionsScreen,
    )
    from remote_agents.domain.remote_control import RemoteControlState

    capable = (
        ProfileResumeCapability(
            ProfileId("claude"), catalogue_available=True, selected_resume_available=True
        ),
    )
    page = ConversationCataloguePage((_summary(),), 1, 1)
    resolved = ResolvedConversation(_summary(), None)  # type: ignore[arg-type]
    return {
        ProjectsScreen: None,  # the resting position, already on the stack
        ProfilesScreen: ProfilesScreen,
        ReviewScreen: ReviewScreen,
        AreasScreen: AreasScreen,
        NameScreen: lambda: NameScreen("infra"),
        ProjectReviewScreen: lambda: ProjectReviewScreen("infra", "new-project"),
        SessionsScreen: SessionsScreen,
        SessionDetailScreen: lambda: SessionDetailScreen(str(_SESSION_ID)),
        RenameScreen: lambda: RenameScreen(str(_SESSION_ID)),
        InspectScreen: lambda: InspectScreen("some output"),
        ResumeProjectsScreen: ResumeProjectsScreen,
        ResumeProfilesScreen: lambda: ResumeProfilesScreen(_PROJECT, capable),
        ResumeConversationsScreen: lambda: ResumeConversationsScreen(_PROJECT, "claude", page),
        ResumeConfirmScreen: lambda: ResumeConfirmScreen(_PROJECT, "claude", resolved),
        ForceConfirmModal: lambda: ForceConfirmModal.for_record(_record()),
        RemoteControlConfirmModal: lambda: RemoteControlConfirmModal.for_change(
            _record(), RemoteControlState.ACTIVE
        ),
    }


def test_every_registered_screen_is_arranged_here() -> None:
    """A screen added to the registry without an arrangement fails, rather than being skipped.

    The exhaustiveness half, for the same reason the back-path suite has one: without it a new
    position would simply never be asked what its footer shows, and this file would stay green
    while covering less than it claims.
    """
    assert set(ALL_SCREENS) == set(_arrangements()), (
        "every screen in ALL_SCREENS needs an arrangement here, and every arrangement "
        "needs to still be registered"
    )


async def _arrange(app: RemoteAgentsTui, pilot, screen_type: type[Screen]) -> None:
    build = _arrangements()[screen_type]
    if build is None:
        return
    await app.push_screen(build())
    await pilot.pause()


@pytest.mark.parametrize("screen_type", ALL_SCREENS, ids=lambda c: c.__name__)
async def test_the_footer_offers_refresh_exactly_where_it_re_reads_something(
    screen_type: type[Screen],
) -> None:
    """Refresh is advertised iff the screen has something to re-read.

    The expectation comes from `can_refresh` — the same declaration `refresh_contents` is
    pinned against in `test_tui_bindings.py` — so this cannot pass by agreeing with a
    `check_action` that has drifted from the hook.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _arrange(app, pilot, screen_type)
        offered = "ctrl+r" in _footer_keys(app)
        expected = bool(getattr(app.screen, "can_refresh", False))

    assert offered is expected, (
        f"{screen_type.__name__} {'offers' if offered else 'hides'} Refresh, but "
        f"can_refresh is {expected}"
    )


@pytest.mark.parametrize("screen_type", ALL_SCREENS, ids=lambda c: c.__name__)
async def test_the_footer_offers_back_everywhere_except_the_resting_position(
    screen_type: type[Screen],
) -> None:
    """Escape is inert when the stack cannot be popped, so it is not advertised there.

    The expectation is read off the stack rather than named per screen: `go_back` refuses on
    the last screen, and that refusal is the rule being mirrored.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _arrange(app, pilot, screen_type)
        offered = "escape" in _footer_keys(app)
        expected = len(app.screen_stack) > 1

    assert offered is expected, (
        f"{screen_type.__name__} {'offers' if offered else 'hides'} Back at stack depth "
        f"{'>1' if expected else '1'}"
    )


@pytest.mark.parametrize("wired", [True, False], ids=["conversations-wired", "no-conversations"])
@pytest.mark.parametrize("screen_type", ALL_SCREENS, ids=lambda c: c.__name__)
async def test_the_footer_offers_resume_only_where_a_conversation_service_exists(
    screen_type: type[Screen], wired: bool
) -> None:
    """`action_resume` returns early without a conversation service; the footer now says so.

    Parametrized over the host configuration rather than asserted on the wired one only,
    because the unwired host is the case that was wrong: `TuiContext.conversations` is
    optional precisely so a host can decline the capability, and the key was advertised there
    all the same.
    """
    app = RemoteAgentsTui(_context(conversations=wired))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _arrange(app, pilot, screen_type)
        offered = "ctrl+o" in _footer_keys(app)
        modal = app.screen.is_modal

    # A modal truncates the binding chain, so no app binding is offered there whatever the
    # host wired — asserted rather than excluded, since "the modal hides everything" is itself
    # a property this stage should not be able to lose silently.
    assert offered is (wired and not modal), (
        f"{screen_type.__name__} {'offers' if offered else 'hides'} Resume with "
        f"conversations {'wired' if wired else 'absent'}"
    )


@pytest.mark.parametrize("screen_type", ALL_SCREENS, ids=lambda c: c.__name__)
async def test_a_modal_offers_no_app_binding_at_all(screen_type: type[Screen]) -> None:
    """The confirmations answer `False` to every app action, and the footer agrees.

    Redundant with the binding-chain truncation and kept anyway: the truncation is Textual's
    behaviour and the `check_action` answer is ours, and a stage that changed either without
    the other should not be able to look unchanged.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _arrange(app, pilot, screen_type)
        if not app.screen.is_modal:
            pytest.skip("not a modal")
        # By namespace, not by key: the modal binds `escape` itself, to answer the question,
        # and that entry legitimately shows. What must not appear is a binding whose namespace
        # is the app — those are the six the truncation and `check_action` both exclude.
        leaked = sorted(
            key for key, active in app.screen.active_bindings.items() if active.node is app
        )

    assert not leaked, f"{screen_type.__name__} still advertises app bindings {leaked}"


@pytest.mark.parametrize("screen_type", ALL_SCREENS, ids=lambda c: c.__name__)
def test_no_screen_inherits_the_permissive_default(screen_type: type[Screen]) -> None:
    """Every position answers for itself, rather than taking Textual's allow-everything.

    The stage gate sweeps for this too. It is here as well because the gate's form asks only
    whether the method differs from `DOMNode`'s — which one definition on `ChoiceScreen`
    satisfies for all sixteen at once. That is the right *implementation* (one rule set, per
    screen data) and the wrong *check*, so the checks above are the ones that would catch a
    screen answering wrongly; this one only catches a screen answering not at all.
    """
    from textual.dom import DOMNode

    assert screen_type.check_action is not DOMNode.check_action, (
        f"{screen_type.__name__} inherits the permissive default and would show every "
        "binding regardless of context"
    )


# --- Task 1.3: a global key must not discard what the owner is typing -------------------

#: Every position where leaving would discard something the owner built — the two that gather
#: typed text, and the two review steps that hold a whole flow's worth of choices. Derived from
#: the property itself rather than listed, so a screen that starts protecting its work is
#: covered here on the same commit.
_WORK_SCREENS = tuple(
    s
    for s in ALL_SCREENS
    if getattr(s, "entry_is_a_commitment", False) or "work_in_flight" in vars(s)
)


def test_every_screen_that_commits_typed_text_declares_it() -> None:
    """`entry_is_a_commitment` and `submit` are two declarations of one fact.

    Same hazard as `can_refresh`/`refresh_contents`, and the same fix: a screen that gathers a
    value with `submit` but forgets the flag lets a global key throw that value away, which is
    precisely what this task exists to stop — and it would do it silently, on the one screen
    nobody remembered to mark.
    """
    from remote_agents.adapters.tui.screens.base import ChoiceScreen

    disagreeing = {
        screen.__name__: (screen.entry_is_a_commitment, hasattr(screen, "submit"))
        for screen in ALL_SCREENS
        if issubclass(screen, ChoiceScreen)
        and screen.entry_is_a_commitment != hasattr(screen, "submit")
    }
    assert not disagreeing, (
        "these screens gather a value and declare it inconsistently — "
        f"(entry_is_a_commitment, has submit) per screen: {disagreeing}"
    )


@pytest.mark.parametrize("binding", ["ctrl+n", "ctrl+s", "ctrl+o"])
@pytest.mark.parametrize("screen_type", _WORK_SCREENS, ids=lambda c: c.__name__)
async def test_a_flow_jump_neither_navigates_nor_loses_work_in_flight(
    screen_type: type[Screen], binding: str
) -> None:
    """The task's own case: press a global key mid-entry and keep both the position and the text.

    Each of these three unwinds the stack to the resting position, so before this rule they
    discarded a half-typed label or project name with no warning and nothing to recover it
    from. Driven with real keys into the real `Input`, because "the value survived" is a claim
    about the widget the owner is typing into, not about a flag.
    """
    from textual.widgets import Input

    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _arrange(app, pilot, screen_type)
        entry = app.screen.query_one("#filter", Input)
        if entry.display:
            entry.focus()
            await pilot.press(*"nightly")
            await pilot.pause()
            assert entry.value == "nightly", "the fixture never typed anything"
        assert app.screen.work_in_flight, "the fixture did not reach a state worth protecting"

        position_before = app.screen.position
        depth_before = len(app.screen_stack)
        await pilot.press(binding)
        await pilot.pause()

        assert app.screen.position == position_before, (
            f"{binding} left {position_before} with text half-typed"
        )
        assert len(app.screen_stack) == depth_before
        if entry.display:
            assert app.screen.query_one("#filter", Input).value == "nightly", (
                f"{binding} discarded the text the owner was typing"
            )


@pytest.mark.parametrize("binding", ["ctrl+n", "ctrl+s", "ctrl+o"])
@pytest.mark.parametrize("screen_type", _WORK_SCREENS, ids=lambda c: c.__name__)
async def test_a_flow_jump_is_greyed_rather_than_hidden_while_text_is_in_flight(
    screen_type: type[Screen], binding: str
) -> None:
    """`None`, not `False` — the key stays drawn and stops working.

    Hiding it would be a second surprise on top of the first: entries vanishing from the footer
    as the owner types is exactly the blinking that the `_busy` rule was left out to avoid. The
    distinction is only observable through `active_bindings`, since both answers stop the
    action, so it is asserted here rather than assumed from the return value.
    """
    from textual.widgets import Input

    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _arrange(app, pilot, screen_type)
        entry = app.screen.query_one("#filter", Input)
        if entry.display:
            entry.focus()
            await pilot.press(*"nightly")
            await pilot.pause()
        assert app.screen.work_in_flight

        active = app.screen.active_bindings
        assert binding in active, f"{binding} vanished from the footer while typing"
        assert not active[binding].enabled, f"{binding} is still live with text in flight"


@pytest.mark.parametrize("binding", ["ctrl+n", "ctrl+s", "ctrl+o"])
async def test_a_flow_jump_still_works_when_the_entry_is_a_filter(binding: str) -> None:
    """The rule is about text that cannot be recovered, and a filter is not that.

    Typing into the project list's filter narrows a list; it is one keystroke to retype and it
    sits on the resting position, where leaving for another flow is the ordinary thing to do.
    Scoping the rule to committed values rather than to "any entry with text in it" is the
    judgment this asserts, so that widening it later is a deliberate change rather than a
    drift.
    """
    from textual.widgets import Input

    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        entry = app.screen.query_one("#filter", Input)
        entry.focus()
        await pilot.press(*"exist")
        await pilot.pause()
        assert entry.value == "exist"
        assert app.screen.position == "PROJECTS"

        await pilot.press(binding)
        await pilot.pause()

        assert app.screen.position != "PROJECTS", (
            f"{binding} was refused on the project filter, where the text is disposable"
        )


#: The positions that protect work, named rather than derived. `_WORK_SCREENS` is computed from
#: the code, so a screen that stopped protecting its work would drop out of that parametrization
#: and take its own coverage with it — the tests would shrink to fit the regression and stay
#: green. Verified: deleting the review screens' override passed every case until this list
#: existed. A literal is the only form that can fail.
_PROTECTS_WORK = {
    "NameScreen",
    "RenameScreen",
    "ReviewScreen",
    "ProjectReviewScreen",
}


def test_exactly_these_positions_protect_work_in_flight() -> None:
    """Which screens have something to lose is a decision, so it is written down.

    Two kinds: the two that gather typed text, and the two review steps that hold a whole
    flow's worth of choices with an empty entry. The second kind is the one a rule written
    against the input widget misses, which is what a stage review found by walking to Review
    with a label committed and pressing Ctrl+S.

    `RenameScreen` joined the first kind rather than being argued out of it. It was written
    declaring no commitment, on the reasoning that its `submit` mutates outright instead of
    carrying the value into a further step — true, and not what the flag turns on:
    `test_every_screen_that_commits_typed_text_declares_it` pins `entry_is_a_commitment ==
    hasattr(screen, "submit")`, and a name being typed is discardable by a global key exactly
    as a project name is.
    """
    # `"work_in_flight" in vars(screen)` was the original predicate and stopped seeing two of
    # these when the launch and project reviews were given a shared `GatheredSelectionScreen`
    # base: an inherited override is not in the subclass's own `__dict__`. What this asks is
    # whether a position protects work in flight, and inheriting the protection is still
    # protecting it — so the comparison is against `ChoiceScreen`'s own default, which is what
    # "overrides it" means. The expected set is unchanged; only the detection was wrong.
    from remote_agents.adapters.tui.screens.base import ChoiceScreen

    actual = {
        screen.__name__
        for screen in ALL_SCREENS
        if getattr(screen, "entry_is_a_commitment", False)
        or (
            issubclass(screen, ChoiceScreen)
            and screen.work_in_flight is not ChoiceScreen.work_in_flight
        )
    }
    assert actual == _PROTECTS_WORK


async def test_the_gathered_launch_survives_a_flow_jump_at_the_review_step() -> None:
    """The journey the review reproduced, driven end to end rather than asserted on a flag.

    Project, then agent, then a label committed with enter — at which point the entry is empty
    and the first version of this rule considered nothing to be in flight. Ctrl+S there
    unwound the stack to the sessions list and the next project choice replaced the selection
    outright, so three screens of the owner's choices were gone with no way back to them.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        app.screen.submit("nightly run")
        await pilot.pause()
        assert app.screen.position == "REVIEW"
        assert app.selection.label == "nightly run"
        depth = len(app.screen_stack)

        await pilot.press("ctrl+s")
        await pilot.pause()

        assert app.screen.position == "REVIEW", "a flow jump discarded the gathered launch"
        assert len(app.screen_stack) == depth
        assert app.selection.label == "nightly run"
        assert app.selection.project is not None and app.selection.profile is not None


async def test_refresh_does_not_discard_the_filter_the_owner_typed() -> None:
    """Refresh re-reads the position; it does not clear it.

    A live instance of this stage's own third clause, and the one the flow-jump rule could not
    reach. The project filter is deliberately exempt from that rule because leaving for another
    flow is the ordinary thing to do here — an argument that does not apply to a key which
    *stays*. `render_projects()` defaults to clearing the entry and moving the keyboard, which
    is right on the way back into this screen and wrong on Ctrl+R.
    """
    from textual.widgets import Input

    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        entry = app.screen.query_one("#filter", Input)
        entry.focus()
        await pilot.press(*"exist")
        await settle_filter(pilot)
        narrowed = [o.id for o in app.screen.query_one("#choices", OptionList).options]
        assert narrowed == ["opaque-existing"], (
            "the filter had not been applied yet, so this test would compare two unfiltered "
            "lists and pass without checking anything"
        )

        await pilot.press("ctrl+r")
        await pilot.pause()

        assert app.screen.query_one("#filter", Input).value == "exist", (
            "Refresh discarded the filter the owner typed"
        )
        assert [o.id for o in app.screen.query_one("#choices", OptionList).options] == narrowed, (
            "Refresh widened the list back out from under the filter"
        )
        assert app.screen.query_one("#filter", Input).has_focus, (
            "Refresh moved the keyboard off the filter the owner was using"
        )
