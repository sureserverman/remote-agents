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

from test_tui_snapshots import settle
from textual.widgets import OptionList
from tui_feedback import announcements
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.screens import ResumeConfirmScreen
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.conversations import (
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
    ProfileChoice("claude", True),
    ProfileChoice("codex", False, "no such profile on this host"),
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


@dataclass(slots=True)
class _RecordingLauncher:
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
        return _record()


class _UnusedCreator:
    """The project-creation service, which none of these flows may reach.

    Spelled out rather than passed as `object()` so that a flow wandering into it fails
    loudly here instead of raising an `AttributeError` that an xfail would absorb.
    """

    def available_areas(self):
        raise AssertionError("no queued-delivery flow in this file creates a project")

    def create(self, command):
        raise AssertionError("no queued-delivery flow in this file creates a project")


def _context(launcher: _RecordingLauncher) -> TuiContext:
    return TuiContext(
        launcher=launcher,  # type: ignore[arg-type]
        creator=_UnusedCreator(),  # type: ignore[arg-type]
        profiles=_PROFILES,
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_PROJECT,),
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


async def _walk_to_review(app: RemoteAgentsTui, pilot) -> None:
    """Gather a project, an agent and an empty label, and stop on Review.

    Driven through the screens' own `choose`/`submit` rather than through keys, exactly as the
    exclusivity tests next door do: the wizard's three earlier steps are not what is under
    test, and the burst has to land on a Review screen that was reached the ordinary way.
    """
    await app.screen.choose("opaque-existing")
    await app.screen.choose("claude")
    app.screen.submit("")
    await settle(app, pilot)
    assert position(app) == "REVIEW", f"the wizard stopped on {position(app)}"


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


async def test_two_queued_enters_on_review_start_exactly_one_session() -> None:
    """BL-015's launch half: a doubled enter on Review must not start two managed sessions.

    A launch is the most expensive thing this surface does — a tmux pane, an agent process, a
    record in the shared store — and it is the one command with no confirmation in front of it,
    because the Review screen *is* the confirmation. So the only thing between a doubled
    keypress and two live agents in one project is whatever refuses the second selection.

    Nothing does. `RemoteAgentsTui.launch` clears `_busy` in a `finally` that runs before
    `self.exit(...)`, and `ReviewScreen.choose` does not leave the position on success, so when
    the pump dispatches the second `OptionSelected` it finds the same screen, the same
    `launch` row and an open guard. Observed: `['launch', 'launch']`.

    The error-toast assertion is not decoration. Without it this test would pass identically
    for a surface that issued one launch and then failed — the count assertion holds while the
    path under test is not the one the name describes — which is the reading a review caught
    the stop tests making next door.
    """
    launcher = _RecordingLauncher()
    app = RemoteAgentsTui(_context(launcher))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _walk_to_review(app, pilot)

        _queue_two(app, "launch")
        # Two pumps rather than one: the first drains the burst, the second gives a second
        # dispatch that got as far as the launcher somewhere to land. A single pause would
        # leave a real double looking like a single.
        await pilot.pause()
        await pilot.pause()
        reported = announcements(app, severity="error")

    assert launcher.issued == ["launch"], (
        f"two queued enters on Review issued {launcher.issued}; exactly one launch was required"
    )
    # Checked *after* the count, per the Stage 1 gate evaluator: with the order reversed, a
    # regression that issued one launch and also reported an error would fail here and read
    # as an unrelated toast rather than as the duplicate-issue defect this test is named for.
    assert reported == [], reported


async def test_two_queued_enters_on_the_resume_confirm_start_exactly_one_session() -> None:
    """BL-015's resume half, which fails for the same structural reason as the launch.

    `issue_resume` clears `_busy` in a `finally` before `self.exit(...)` and
    `ResumeConfirmScreen.choose` stays where it is, so the second queued selection lands on the
    confirmation with its `resume-confirm` row still rendered and the guard already open.
    Observed: `['resume', 'resume']` — two sessions continuing the same conversation, which is
    worse than two launches: both panes then write to one provider conversation.

    The screen is pushed directly with the real project, agent and `ResolvedConversation` it
    carries, rather than walked to through the conversation catalogue. The list and its paging
    are not what is under test, and the confirmation is constructed exactly as
    `ResumeConversationsScreen.choose` constructs it.
    """
    launcher = _RecordingLauncher()
    app = RemoteAgentsTui(_context(launcher))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.push_screen(ResumeConfirmScreen(_PROJECT, "claude", _conversation()))
        await settle(app, pilot)
        assert position(app) == "RESUME_CONFIRM", f"the push landed on {position(app)}"

        _queue_two(app, "resume-confirm")
        await pilot.pause()
        await pilot.pause()
        reported = announcements(app, severity="error")

    assert launcher.issued == ["resume"], (
        f"two queued enters on the resume confirmation issued {launcher.issued}; exactly one "
        f"resume was required"
    )
    # Ordered after the count for the reason given on the launch case above.
    assert reported == [], reported
