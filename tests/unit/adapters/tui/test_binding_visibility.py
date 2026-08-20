"""The footer lists the keys that do something here, and nothing else.

Six app-level bindings were shown on every position regardless of whether they applied.
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
from textual.widgets import Input, OptionList
from tui_feedback import announcements
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
        DashboardScreen,
        FeedScreen,
        ForceConfirmModal,
        InspectScreen,
        NameScreen,
        ProfilesScreen,
        ProjectChooserScreen,
        ProjectReviewScreen,
        ProjectsPaneScreen,
        RemoteControlConfirmModal,
        RenameScreen,
        ResumeConversationsScreen,
        ResumeProfilesScreen,
        ResumeProjectsScreen,
        ReviewScreen,
        SessionDetailScreen,
        SessionsPaneScreen,
        SessionsScreen,
    )
    from remote_agents.domain.remote_control import RemoteControlState

    capable = (
        ProfileResumeCapability(
            ProfileId("claude"), catalogue_available=True, selected_resume_available=True
        ),
    )
    page = ConversationCataloguePage((_summary(),), 1, 1)
    return {
        DashboardScreen: None,  # the resting position, already on the stack
        ProfilesScreen: ProfilesScreen,
        ProjectChooserScreen: lambda: ProjectChooserScreen(_PROJECT),
        ReviewScreen: ReviewScreen,
        AreasScreen: AreasScreen,
        NameScreen: lambda: NameScreen("infra"),
        ProjectReviewScreen: lambda: ProjectReviewScreen("infra", "new-project"),
        SessionsScreen: SessionsScreen,
        # The console's three pane positions, pushed for the same reason the back-path
        # suite pushes them: this app rests on the dashboard, and what is asked of them
        # here is what their footer advertises, which is a property of the screen.
        ProjectsPaneScreen: ProjectsPaneScreen,
        SessionsPaneScreen: SessionsPaneScreen,
        FeedScreen: FeedScreen,
        SessionDetailScreen: lambda: SessionDetailScreen(str(_SESSION_ID)),
        RenameScreen: lambda: RenameScreen(str(_SESSION_ID)),
        InspectScreen: lambda: InspectScreen("some output"),
        ResumeProjectsScreen: ResumeProjectsScreen,
        ResumeProfilesScreen: lambda: ResumeProfilesScreen(_PROJECT, capable),
        ResumeConversationsScreen: lambda: ResumeConversationsScreen(_PROJECT, "claude", page),
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
    satisfies for all fifteen at once. That is the right *implementation* (one rule set, per
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
        assert app.screen.position == "DASHBOARD"

        await pilot.press(binding)
        await pilot.pause()

        assert app.screen.position != "DASHBOARD", (
            f"{binding} was refused on the project filter, where the text is disposable"
        )


@pytest.mark.parametrize("binding", ["ctrl+n", "ctrl+s", "ctrl+o"])
async def test_a_flow_jump_still_works_on_the_launch_review(binding: str) -> None:
    """The launch review stopped protecting work, because its work stopped being unrecoverable.

    It held a gathered selection *plus a typed label*, and the label was the part escape could
    not give back — the label entry cleared itself on the way in, so walking back lost it. With
    that step gone the review holds two list selections, and escape lands on the agent
    list with both lists still there: re-picking is two keystrokes, which is the same reasoning
    that exempts the project filter above.

    The project review is deliberately *not* exempted with it, and the difference is the test of
    whether this is a principled narrowing or a convenient one: that screen holds a typed project
    name, `NameScreen.populate` clears it too, and so it keeps the protection this one loses.
    `_PROTECTS_WORK` below is what pins the pair.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("launch")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        assert app.screen.position == "REVIEW", f"the walk landed on {app.screen.position}"

        active = app.screen.active_bindings
        assert binding in active, f"{binding} is not offered on the review at all"
        assert active[binding].enabled, (
            f"{binding} is greyed on the review, which now holds nothing escape cannot give back"
        )


async def test_quit_at_the_launch_review_leaves_on_the_first_press_and_that_is_deliberate() -> None:
    """The consequence of the narrowing that its own commit did not discuss, pinned so it is a
    decision rather than a side effect.

    Dropping `GatheredSelectionScreen` from the launch review disarms two things, not one. The
    flow-jump greying is the half that was reasoned about; `ctrl+q`'s arm-then-warn cycle reads
    the *same* `work_in_flight` property, so it went quiet here too and nothing said so. A
    Tier-2 review found the omission and was right to: undiscussed is undiscussed even when the
    outcome is correct.

    It **is** correct, and for the reason the whole narrowing rests on. DEC-027's warning exists
    for work the owner cannot get back — its own text is about "the owner's own unsaved text".
    The launch review holds two list selections and nothing typed, so quitting costs two
    re-picks from lists that are still there next launch. Warning about that would train the
    owner to dismiss the warning, which is what makes the one guarding a typed project name
    worth less.

    The control case is asserted alongside, because a test that only showed the absence could
    pass just as well if the warning had broken everywhere: the project review, which holds a
    typed name, still warns first and still leaves on the second press.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("launch")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        assert app.screen.position == "REVIEW"
        assert not app.screen.work_in_flight, "the review is still protecting work"

        await pilot.press("ctrl+q")
        await pilot.pause()
        warned = announcements(app)

    assert warned == [], f"quit warned where there is nothing to lose: {warned}"
    assert not app.is_running, "quit did not leave on the first press"


async def test_quit_still_warns_first_where_a_typed_name_is_at_risk() -> None:
    """The control for the test above: the flow that still has work still gets the warning.

    Asserted in the same file and next to it on purpose. The claim being made is not "quit no
    longer warns" but "quit warns exactly where something is at risk", and only the pair can
    say that. DEC-027's shape is checked to the end — the second press always leaves, so this
    is a warning and never a refusal.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.show_areas()
        await pilot.pause()
        await app.screen.choose("infra")
        await pilot.pause()
        assert app.screen.position == "NAME"
        app.screen.query_one("#filter", Input).focus()
        await pilot.press(*"orbit")
        await pilot.pause()

        await pilot.press("ctrl+q")
        await pilot.pause()
        warned = announcements(app)
        still_running = app.is_running

        await pilot.press("ctrl+q")
        await pilot.pause()

    assert warned, "a typed project name was discarded with no warning at all"
    assert "orbit" in warned[0], f"the warning did not name what was at risk: {warned}"
    assert still_running, (
        "the first press left rather than warning — that is a discard, not a warning"
    )
    assert not app.is_running, "the second press did not leave, which would make it a refusal"


#: The positions that protect work, named rather than derived. `_WORK_SCREENS` is computed from
#: the code, so a screen that stopped protecting its work would drop out of that parametrization
#: and take its own coverage with it — the tests would shrink to fit the regression and stay
#: green. Verified: deleting the review screens' override passed every case until this list
#: existed. A literal is the only form that can fail.
_PROTECTS_WORK = {
    "NameScreen",
    "RenameScreen",
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


async def test_a_flow_jump_at_the_review_now_leaves_and_the_cost_is_two_reselections() -> None:
    """The inverse of what this asserted, recorded as a deliberate change rather than deleted.

    **Its premise was the label, and the label is gone.** It read: project, then agent, then a
    label committed with enter — "at which point the entry is empty and the first version of
    this rule considered nothing to be in flight". Ctrl+S there unwound the stack and the next
    project choice replaced the selection outright, so three screens of choices went with no way
    back to them. The protection was bought for the typed label sitting invisibly behind an
    empty entry.

    With that step removed the review holds two list selections and nothing typed, so the jump
    is allowed and this test records what it costs: the owner lands on the sessions list, and
    getting back to a launch means re-picking a project and an agent — two selections from two
    lists that are both still there. That is the same trade the project filter has always made.

    Asserted rather than assumed, because it *is* a loss, just a small and recoverable one. If a
    future step puts typed work back into this flow, this test is where the argument has to be
    reopened.
    """
    app = RemoteAgentsTui(_context())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("launch")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        assert app.screen.position == "REVIEW"
        assert app.selection.project is not None and app.selection.profile is not None

        await pilot.press("ctrl+s")
        await pilot.pause()

        assert app.screen.position == "SESSIONS", (
            "the jump was refused at the review, which is the protection this step no longer "
            "has anything to protect"
        )
        # The way back, and the whole reason the loss is acceptable: the resting position is one
        # escape away and both lists are intact.
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.position == "DASHBOARD"
        keys = [option.id for option in app.screen.query_one("#choices", OptionList).options]
        assert "opaque-existing" in keys, (
            f"the project must still be there to re-pick from; the list offered {keys}"
        )

        # And the leg a Tier-2 review asked for: walk the recovery to its end rather than
        # stopping at "the list is still there". `ProjectsScreen.choose` builds a *fresh*
        # `LaunchSelection` rather than patching the old one, so nothing from the abandoned pass
        # can survive into the new review — provable from the source, and now demonstrated.
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("launch")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        assert app.screen.position == "REVIEW", "the recovery did not reach a fresh review"
        assert app.selection.project is not None
        assert app.selection.profile is not None
        assert app.selection.profile.profile_id == "claude"


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
