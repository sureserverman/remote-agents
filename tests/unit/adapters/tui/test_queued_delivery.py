"""Two fast enters on a launch or a resume must start one session, not two.

**Queued delivery, not concurrent delivery, and the distinction is the whole file.** A repeated
keypress is not two coroutines in flight at once: `OptionList` *posts* `OptionSelected`, and
Textual serialises handlers on the message pump, so the second is dispatched only after the
first has returned — by which point the `_busy` flag the concurrent tests rely on has already
been cleared by that handler's `finally`. Every exclusivity test in
`test_tui_worker_exclusivity.py` except the graceful-stop one drives the concurrent shape, which
is the shape the framework never produces from a keyboard.

That file's module docstring claims launch and resume are unprotected under queued delivery,
and records it as BL-015: both end in `self.exit(...)` without leaving the position, so the
second enter finds the same screen, a cleared flag and the same row, and issues again. The
claim predates the Textual screen rewrite and nobody had checked whether the rewrite changed
it. **It did not.** Driven here, two queued selections on Review issue two launches, and two on
the resume confirmation issue two resumes — verbatim `['launch', 'launch']` and
`['resume', 'resume']`. Both properties are therefore asserted as `xfail(strict=True)`: the
assertions state what the surface must do, they fail today, and Task 2.1 closes them by
deleting the markers rather than by rewriting the tests.

`strict=True` matters more than usual here. A test whose whole body is inside an xfail envelope
hides *why* it failed — a version that issued no command at all would look identical to a
version that issued two — so two things guard against that reading. The strict marker turns an
unexpected pass into a failure, so a fix that made these green by accident cannot land quietly;
and `test_a_queued_burst_reaches_the_screen_twice` below pins the delivery mechanism itself
outside any xfail, so "the second message was never dispatched" is refuted by a test that stays
green through Task 2.1.

The defect is also pinned by mutation rather than assumed. Holding the busy guard across the
`self.exit(...)` in `RemoteAgentsTui.launch` and `RemoteAgentsTui.issue_resume` — replacing each
`finally: self._busy = False` with a no-op, which is one plausible shape of the Task 2.1 fix —
makes both of these pass. So the assertions below are sensitive to exactly the guard they are
about, in both directions, and the source change was reverted before this file was committed.

Nothing here is timed. The queued model needs no slow fake at all: the second message *cannot*
be dispatched until the first handler returns, so `_RecordingLauncher` answers immediately and
the burst is still a burst. `_SlowLauncher`'s started/release handshake next door exists to hold
a window open for the concurrent shape, and reaching for it here would only disguise a
synchronous ordering as a race.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from backends import SessionUseCaseDouble, backend_for
from test_tui_snapshots import settle
from textual.widgets import OptionList
from tui_feedback import announcements
from tui_positions import position

from remote_agents.adapters.tui.app import AttachRequest, RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.screens import ResumeConversationsScreen
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.services import ResumeOutcome
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProviderConversationId,
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

#: The agent the launch flow picks, and one it cannot pick. The unavailable entry is not
#: decoration: it is what the delivery control below selects, because refusing an unavailable
#: agent is the one row in this surface that says something out loud and then leaves the owner
#: exactly where they were — so a second dispatch of it is *visible* rather than idempotent.
_PROFILES = (
    ProfileAvailability("claude", True),
    ProfileAvailability("codex", False, "no such profile on this host"),
)


def _record(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        _SESSION_ID,
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


def _conversation() -> ResolvedConversation:
    """A real resolved conversation, because a stand-in would make this file vacuous.

    `ResumeCommand.__post_init__` validates what it is handed, so passing a bare `object()`
    raises before the service is reached — the test then records "no command issued" and passes
    with no guard at all. That is not a hypothetical: it is the vacuity
    `test_tui_worker_exclusivity.py` records catching in its own resume test, and under the
    xfail envelope here it would be even quieter, since a construction error and a live defect
    both read as "expected failure".
    """
    summary = ConversationSummary(
        ConversationReference("c-" + "0" * 14 + "01"),
        ProfileId("claude"),
        ProjectId("opaque-existing"),
        ConversationState.RESUMABLE,
        datetime.now(UTC),
        description="a saved conversation",
    )
    return ResolvedConversation(summary, ProviderConversationId("abc123"))


class _Resolving:
    """The one conversation-service method the resume act needs, and nothing else.

    The confirmation this file used to push already *held* its `ResolvedConversation`, so the
    context needed no conversation service at all. Choosing on the list resolves first, so the
    service is now part of reaching the act — wired here rather than in `_context`'s defaults
    because only the two resume tests need it.
    """

    async def resolve_for_resume(self, reference: ConversationReference) -> ResolvedConversation:
        resolved = _conversation()
        assert reference == resolved.summary.reference, (
            f"the screen resolved {reference}, which is not the row it was given"
        )
        return resolved


def _conversation_page() -> ConversationCataloguePage:
    """One page holding exactly the conversation `_conversation()` resolves to.

    The resume act used to live on a confirmation this file pushed directly; it lives on the
    conversation list now, so reaching it means standing on a real page. Built from the same
    summary the resolve answers with, so the row the test selects and the command it produces
    describe one conversation rather than two that happen to look alike.
    """
    return ConversationCataloguePage((_conversation().summary,), 1, 1)


@dataclass(slots=True)
class _RecordingLauncher(SessionUseCaseDouble):
    """Records every command and answers at once.

    Deliberately not slow. See the module docstring: under queued delivery the second handler
    cannot start until the first has returned, so a held-open window would prove nothing that
    the pump's own ordering does not already guarantee.
    """

    issued: list[str] = field(default_factory=list)

    async def refresh_readiness(self):
        return (_record(),)

    async def list_sessions(self):
        return (_record(),)

    async def copy_attach(self, _session_id):
        return None

    async def launch(self, _command):
        self.issued.append("launch")
        return _record()

    async def resume(self, _command):
        self.issued.append("resume")
        return ResumeOutcome(_record(), created=True)


class _FailingLauncher(_RecordingLauncher):
    """Records every command, and answers that the session never became ready.

    The distinction from `_RecordingLauncher` is the whole point of the test below.
    `RemoteAgentsTui.launch` clears `_busy` in a `finally` and only ever sets `_leaving` on
    the *success* path, so a launcher that succeeds exercises a guard a launcher that fails
    does not have. Every queued-delivery test in this file used the succeeding double until a
    Stage 1 gate review pointed that out.
    """

    async def launch(self, _command):
        self.issued.append("launch")
        return _record(SessionState.FAILED)


class _UnusedCreator:
    """The project-creation service, which none of these flows may reach.

    Spelled out rather than passed as `object()` so that a flow wandering into it fails
    loudly here instead of raising an `AttributeError` that an xfail would absorb.
    """

    def available_areas(self):
        raise AssertionError("no queued-delivery flow in this file creates a project")

    def create(self, command):
        raise AssertionError("no queued-delivery flow in this file creates a project")


def _context(launcher: _RecordingLauncher, *, conversations: object | None = None) -> TuiContext:
    return TuiContext(
        backend=backend_for(
            conversations=conversations,  # type: ignore[arg-type]
            sessions=launcher,  # type: ignore[arg-type]
            projects=_UnusedCreator(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_PROJECT,),
            catalogue=(_PROJECT,),
        ),
        profiles=_PROFILES,
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


def _queue_two(app: RemoteAgentsTui, key: str) -> None:
    """Put two selections of the same row into the pump in one turn.

    `Pilot.press` cannot express this and never could: it awaits idle between keys, so the
    first handler has finished *and the screen has repainted* before the second key exists.
    `test_confirm_modals.py` records the same limitation and the same workaround — key
    autorepeat, a double tap, and buffered stdin over a laggy link all hand the driver one read
    that parses into several key events posted back to back, which is what this reproduces.

    Posted to the **screen**, not to the app. `on_option_list_option_selected` lives on
    `ChoiceScreen` after the rewrite and a bubbling selection is consumed there, so messages
    posted to the app would sit in a queue nothing handles — and both of the tests below would
    then report "exactly one command" while never delivering a second event at all. That is the
    quiet way this file could have been vacuous, so the target is named here once.
    """
    screen = app.screen
    choices = screen.query_one("#choices", OptionList)
    index = choices.get_option_index(key)
    chosen = choices.get_option_at_index(index)
    # No await between them, which is what a single terminal read delivers.
    screen.post_message(OptionList.OptionSelected(choices, chosen, index))
    screen.post_message(OptionList.OptionSelected(choices, chosen, index))


async def _walk_to_the_agent_list(app: RemoteAgentsTui, pilot) -> None:
    """Gather a project, and stop on the agent list — which is where the launch is issued.

    Driven through the screens' own `choose` rather than through keys, exactly as the
    exclusivity tests next door do: the wizard's earlier steps are not what is under test, and
    the burst has to land on a screen that was reached the ordinary way.

    **One choice, not three.** The flow lost its label step and then its review step, so the
    agent list is the commit position: choosing a row here *is* the launch, which is why the
    burst below is queued on an agent row rather than on a `launch` row.
    """
    await app.screen.choose("opaque-existing")
    await app.screen.choose("launch")
    await settle(app, pilot)
    assert position(app) == "PROFILES", f"the wizard stopped on {position(app)}"


async def test_a_queued_burst_reaches_the_screen_twice() -> None:
    """The delivery mechanism itself, pinned outside any xfail envelope.

    Both tests below assert "exactly one command", and both are expected to fail today. That
    combination has a silent failure mode: a burst that was never dispatched at all would fail
    the same assertions for a completely different reason, and once Task 2.1 deletes the
    markers it would make them *pass* while proving nothing. So the mechanism is asserted
    separately, on a row whose second dispatch is observable and whose behaviour Task 2.1 does
    not touch.

    An unavailable agent is that row. `ProfilesScreen.choose` announces why it cannot be
    launched and returns without navigating and without ever setting the busy guard — so the
    screen is still on top and still unguarded when the second message is dispatched, and two
    toasts is proof that both events reached the handler. One toast would mean this file's
    other two tests are asking their question of a pump that only ever answered once.
    """
    app = RemoteAgentsTui(_context(_RecordingLauncher()))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await app.screen.choose("launch")
        await settle(app, pilot)
        assert position(app) == "PROFILES", f"the wizard stopped on {position(app)}"

        _queue_two(app, "codex")
        await settle(app, pilot)
        await pilot.pause()

        assert position(app) == "PROFILES", "a refused agent must leave the owner where they were"
        refusals = announcements(app, severity="warning")

    assert len(refusals) == 2, (
        f"two queued selections were refused {len(refusals)} time(s): {refusals}. Only one "
        f"reached the handler, so the queued burst this file is built on is not being delivered "
        f"and the launch and resume tests below prove nothing."
    )


async def test_two_queued_enters_on_an_agent_start_exactly_one_session() -> None:
    """BL-015's launch half: a doubled enter on an agent must not start two managed sessions.

    A launch is the most expensive thing this surface does — a tmux pane, an agent process, a
    record in the shared store — and it now has no confirmation in front of it at all, because
    the review step that was standing there guarded nothing and was removed. So the only thing
    between a doubled keypress and two live agents in one project is whatever refuses the
    second selection.

    **What refuses it on this path is `_leaving`** — `RemoteAgentsTui.launch` clears `_busy` in
    a `finally` that runs before `self.exit(...)`, and the screen that issued the launch does
    not leave the position on success, so the guard the second dispatch meets cannot be
    `_busy`. `_leave` sets `_leaving`, `busy` reads `self._busy or self._leaving`, and nothing
    clears it. That guard belongs to the app rather than to any screen, which is why the act
    could move between screens without the protection moving with it. Observed before it
    existed: `['launch', 'launch']`.

    **"On this path" is doing real work in that sentence, and an earlier version of it did not
    say so.** It read that `_leaving` "is why removing the review did not reopen this", full
    stop — which is false for a launch that *fails*, since `_leaving` is only ever set on the
    success path. This double answers `RUNNING`, so this test never reached that branch and the
    claim went unchecked through two reviews.
    `test_two_queued_enters_on_a_failed_launch_start_exactly_one_session` covers it, with a
    double that fails.

    The error-toast assertion is not decoration. Without it this test would pass identically
    for a surface that issued one launch and then failed — the count assertion holds while the
    path under test is not the one the name describes — which is the reading a review caught
    the stop tests making next door.
    """
    launcher = _RecordingLauncher()
    app = RemoteAgentsTui(_context(launcher))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _walk_to_the_agent_list(app, pilot)

        _queue_two(app, "claude")
        # Two pumps rather than one: the first drains the burst, the second gives a second
        # dispatch that got as far as the launcher somewhere to land. A single pause would
        # leave a real double looking like a single.
        await pilot.pause()
        await pilot.pause()
        reported = announcements(app, severity="error")

    assert launcher.issued == ["launch"], (
        f"two queued enters on an agent issued {launcher.issued}; exactly one launch was required"
    )
    # Checked *after* the count, per the Stage 1 gate evaluator: with the order reversed, a
    # regression that issued one launch and also reported an error would fail here and read
    # as an unrelated toast rather than as the duplicate-issue defect this test is named for.
    assert reported == [], reported


async def test_two_queued_enters_on_a_conversation_start_exactly_one_session() -> None:
    """BL-015's resume half, which fails for the same structural reason as the launch.

    `issue_resume` clears `_busy` in a `finally` before `self.exit(...)` and the screen that
    called it stays where it is, so the second queued selection lands on a list with the same
    conversation row still rendered and the guard already open. Observed: `['resume',
    'resume']` — two sessions continuing the same conversation, which is worse than two
    launches: both panes then write to one provider conversation.

    **The screen this stands on changed and the defect did not.** It used to be the resume
    confirmation, pushed directly with its resolved conversation; that step is gone and
    choosing a row on the conversation list is now the act itself, so this pushes the list
    instead. Pushed rather than walked to, for the same reason as before: the catalogue and its
    paging are not what is under test.
    """
    launcher = _RecordingLauncher()
    app = RemoteAgentsTui(_context(launcher, conversations=_Resolving()))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.push_screen(ResumeConversationsScreen(_PROJECT, "claude", _conversation_page()))
        await settle(app, pilot)
        assert position(app) == "RESUME_CONVERSATIONS", f"the push landed on {position(app)}"

        _queue_two(app, str(_conversation().summary.reference))
        await pilot.pause()
        await pilot.pause()
        reported = announcements(app, severity="error")

    assert launcher.issued == ["resume"], (
        f"two queued enters on the resume confirmation issued {launcher.issued}; exactly one "
        f"resume was required"
    )
    # Ordered after the count for the reason given on the launch case above.
    assert reported == [], reported


async def test_quit_during_the_leaving_window_keeps_the_attach_request() -> None:
    """`ctrl+q` after a successful launch must not throw away what the launch returned.

    Found by the Stage 2 gate's Tier-2 pass, which is the first review to see Task 2.1 and
    Task 2.2 together — and this defect exists only in their interaction, so neither
    per-task review could have caught it.

    `_leave` sets `_leaving` and calls `self.exit(request)`, and its own docstring says the
    surface must stop answering until the app is actually gone. `action_quit` was written in
    the same stage and never consulted that flag: `check_action` deliberately exempts quit
    from every rule, on the argument that an app which cannot be closed is worse than one
    that loses work. That argument predates `_leaving` and does not reach this case.

    Textual's `App.exit()` overwrites `_return_value` unconditionally, and `App.action_quit`
    calls it with no argument — so a `ctrl+q` landing in the teardown window replaced a live
    `AttachRequest` with `None`. The composition root then attaches to nothing, and the
    session the owner just started is left running with no handle offered. Silent, on the
    success path, with the work already done.

    Both flows are driven because they used to arm differently: the launch review's
    `work_in_flight` was hardcoded `True`, so the launch case needed a second press to reach
    the clobber, while the resume screen holds no entry and is unarmed, so one press did it.
    That review stopped protecting work before it was removed altogether, and the agent list
    that inherited the launch never claimed to — so the two arm the same way now. The second
    press below is harmless either way and is kept because the clobber is what is under test,
    not the number of presses it takes to reach it.
    """
    launcher = _RecordingLauncher()
    app = RemoteAgentsTui(_context(launcher))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _walk_to_the_agent_list(app, pilot)
        await app.screen.choose("claude")
        assert app._leaving, "the launch did not reach `_leave`, so there is no window to test"
        launched = app.return_value
        assert isinstance(launched, AttachRequest), (
            f"the launch should have handed back an attach request, got {launched!r}"
        )

        # Twice: the first arms the quit warning on this screen, the second is the press the
        # owner would make to answer it — and it is the one that used to clobber.
        await app.action_quit()
        await app.action_quit()

        assert app.return_value == launched, (
            f"quitting inside the leaving window replaced the pending attach request with "
            f"{app.return_value!r}; the session is running and nothing will attach to it"
        )


async def test_quit_during_the_leaving_window_keeps_the_resumed_attach_request() -> None:
    """The resume half, which needs only one press because its screen holds no entry.

    `ResumeConversationsScreen` neither sets `entry_is_a_commitment` nor overrides
    `work_in_flight`, so the quit warning never arms and the very first `ctrl+q` goes straight
    through to `App.action_quit`. That was true of the resume confirmation this replaces, for
    the same reason and with the same consequence.
    """
    launcher = _RecordingLauncher()
    app = RemoteAgentsTui(_context(launcher, conversations=_Resolving()))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.push_screen(ResumeConversationsScreen(_PROJECT, "claude", _conversation_page()))
        await settle(app, pilot)
        await app.screen.choose(str(_conversation().summary.reference))
        assert app._leaving, "the resume did not reach `_leave`"
        resumed = app.return_value
        assert isinstance(resumed, AttachRequest), (
            f"the resume should have handed back an attach request, got {resumed!r}"
        )

        await app.action_quit()

        assert app.return_value == resumed, (
            f"quitting inside the leaving window replaced the resumed attach request with "
            f"{app.return_value!r}"
        )


async def test_two_queued_enters_on_a_failed_launch_start_exactly_one_session() -> None:
    """The half of BL-015 that `_leaving` never covered, and that three comments claimed it did.

    **The guard the success path relies on does not exist on the failure path.**
    `RemoteAgentsTui.launch` clears `_busy` in a `finally` that runs before its
    `SessionState.FAILED` return, and `_leaving` is set only inside `_leave`, which a failure
    never reaches. So the second selection of a queued pair meets an open guard, and the first
    launch has already failed by then — leaving two live panes in one project, with two
    distinct idempotency keys, so no backend can dedupe them. The toast the owner is reading
    while it happens says "a second session will run alongside it".

    **Not a regression, which is why it needed a test rather than a revert.** The same burst
    against the review screen this flow used to end on issues `['launch', 'launch']` too — the
    exposure predates the position moving, and both surfaces need one deliberate cursor move
    off the resting row first. What changed is that the code and this file had begun asserting
    the hole was closed.

    What closes it is the invariant the cursor was already carrying: **a commit position
    commits the row the cursor is on, and after a failed launch the cursor is on nothing.** The
    failure re-render rests it on `None` (`show_choices(highlight=None)`), so every selection
    queued against the fill that is now gone is refused by `ProfilesScreen.choose` — which is
    what makes that re-render load-bearing rather than cosmetic.
    """
    launcher = _FailingLauncher()
    app = RemoteAgentsTui(_context(launcher))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _walk_to_the_agent_list(app, pilot)

        _queue_two(app, "claude")
        await pilot.pause()
        await pilot.pause()
        cursor = app.screen.query_one("#choices", OptionList).highlighted
        reported = announcements(app, severity="error")

    assert launcher.issued == ["launch"], (
        f"two queued enters on a failing launch issued {launcher.issued}; exactly one was "
        f"required, and each extra one is a live agent pane the owner never asked for"
    )
    # The cursor is the mechanism, so it is asserted rather than assumed: a regression that
    # restored a resting row would re-open the hole while the count above still read one, on
    # any run where the second event happened to lose the race.
    assert cursor is None, f"the cursor rests on row {cursor} after a failed launch"
    assert len(reported) == 1, f"one failure, {len(reported)} reports: {reported}"
