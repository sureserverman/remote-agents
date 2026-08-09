"""A repeated keypress issues one command, and quitting leaves nothing running.

Two guarantees this project relied on and had never tested.

The first is DEC-007's: a repeated enter must not destroy anything twice. **Which mechanism
delivers that depends on how the second enter arrives, and the two are not the same test.**
Concurrently — two coroutines in flight at once — the `_busy` flag refuses the second at
`on_option_list_option_selected`. Queued, which is what two fast keypresses actually produce because
Textual serialises handlers on the pump, `_busy` has already been cleared by the first
handler's `finally`, and what refuses the second stop is `_stop` re-reading the record and
re-checking the policy. Both are covered below, and they are labelled for which is which.

Neither mechanism protects **launch or resume** under queued delivery: both end in
`self.exit(...)` without leaving the position, so the second enter finds the same screen
and a cleared flag and issues again. Two fast enters on Review start two managed sessions. That is
a live defect, pre-existing and outside this plan's blast radius — BL-015.

Whatever the mechanism, cancel-and-restart is wrong for a destructive action: it would mean
the profile's exit sequence has already reached the pane, the kill abandoned midway, and a
second issued. That is what DEC-008 records.

The second is the property Task 2.1 bought by moving the blocking calls onto app-owned
workers: quitting while one is in flight must leave no thread running and no coroutine
unawaited, rather than a thread writing into a torn-down screen.

Neither is asserted anywhere else, so a refactor could quietly remove either.
`test_tui_force_stop.py` covers the *cursor* half of DEC-007's mitigation — that no screen
rests on a mutating row — and this file covers the *concurrency* half.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest
from test_tui_snapshots import settle
from textual.widgets import OptionList
from textual.worker import WorkerState
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.screens import ResumeConfirmScreen
from remote_agents.application.project_admin import CreatedProject
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
from remote_agents.domain.projects import ProjectIdentity
from remote_agents.domain.remote_control import RemoteControlState

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_SESSION_ID = SessionId.new()


def _record(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        _SESSION_ID,
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _SlowLauncher:
    """Records every command, and holds the first one open until the test releases it.

    The window has to be genuinely open or these tests prove nothing — if the first call
    completed before the second keypress arrived, the assertions would hold for a surface
    with no guard at all. An earlier version opened it with `sleep(0.2)` and raced it with
    `sleep(0.05)`, which held on an idle machine and failed under load: the exact flake this
    file is meant to be evidence against. `started`/`release` make it a synchronisation
    point instead, with no wall-clock dependence at all.
    """

    issued: list[str] = field(default_factory=list)
    state: SessionState = SessionState.RUNNING
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def _record_and_wait(self, name: str):
        self.issued.append(name)
        self.started.set()
        await self.release.wait()
        return _record(self.state)

    async def refresh_readiness(self):
        return (_record(self.state),)

    async def list_sessions(self):
        return (_record(self.state),)

    async def copy_attach(self, _session_id):
        return None

    async def launch(self, _command):
        return await self._record_and_wait("launch")

    async def resume(self, _command):
        return await self._record_and_wait("resume")

    async def graceful_stop(self, _command):
        await self._record_and_wait("graceful")

    async def cleanup(self, _command):
        await self._record_and_wait("cleanup")

    async def force_stop(self, _command):
        await self._record_and_wait("force")

    async def set_remote_control(self, _command):
        await self._record_and_wait("remote-control")
        return RemoteControlState.ACTIVE


@dataclass(slots=True)
class _SlowCatalogue:
    """A blocking catalogue read, held open so a worker is RUNNING when the app exits."""

    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def __call__(self):
        self.started.set()
        self.release.wait(timeout=5)
        return (_PROJECT,)


@dataclass(slots=True)
class _SlowCreator:
    issued: list[str] = field(default_factory=list)
    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def available_areas(self):
        return ("dev-area", "infra")

    def create(self, command):
        # Runs on a worker thread, so the handshake is threading's rather than asyncio's.
        self.issued.append("create")
        self.started.set()
        self.release.wait(timeout=5)
        return CreatedProject(ProjectIdentity(command.area, command.name), None)  # type: ignore[arg-type]


def _context(launcher: _SlowLauncher, creator: _SlowCreator | None = None) -> TuiContext:
    return TuiContext(
        launcher=launcher,  # type: ignore[arg-type]
        creator=creator or _SlowCreator(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_PROJECT,),
    )


async def _select(app: RemoteAgentsTui, key: str) -> None:
    """Deliver a row selection the way a keypress does — through the real handler.

    This matters, and getting it wrong is how the first draft of this file reported a bug
    that does not exist. The guard is **not** uniformly placed: `_stop` checks `_busy`
    itself, but the launch and the project creation only *set* it and rely on the
    selection handler to have refused the second event. Calling those resolvers directly
    therefore walks straight past the protection and issues two commands.

    So these tests go through the handler. **The screen rewrite moved that dispatch onto
    `ChoiceScreen`, and the caller-side check moved with it** — this helper now routes
    through the active screen, which is where the `busy` refusal lives. That was the risk
    this docstring named while the handler was still on the app: a rewrite that dropped the
    check would have left launch and create unguarded while the stops stayed safe, and this
    file is what would have caught it.
    """
    from textual.widgets import OptionList

    choices = app.screen.query_one("#choices", OptionList)
    index = choices.get_option_index(key)
    await app.screen.on_option_list_option_selected(
        OptionList.OptionSelected(choices, choices.get_option_at_index(index), index)
    )


async def _drive_to_force_confirm(app: RemoteAgentsTui) -> None:
    await app.show_sessions()
    await app.show_detail(str(_SESSION_ID))
    await app.screen.confirm_force()


@pytest.mark.parametrize(
    "state,resolve,expected",
    [
        pytest.param(SessionState.RUNNING, "force-confirm", "force", id="force"),
        pytest.param(SessionState.RUNNING, "graceful", "graceful", id="graceful"),
        pytest.param(SessionState.PRESERVED, "cleanup", "cleanup", id="cleanup"),
    ],
)
async def test_a_repeated_keypress_issues_exactly_one_stop(state, resolve, expected) -> None:
    """DEC-007: a second enter while a stop is in flight must not issue a second stop."""
    launcher = _SlowLauncher(state=state)
    app = RemoteAgentsTui(_context(launcher))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.show_sessions()
        await app.show_detail(str(_SESSION_ID))
        await pilot.pause()

        if resolve == "force-confirm":
            await app.screen.confirm_force()
            await pilot.pause()
            first = asyncio.create_task(app.screen.choose("force-confirm"))
            await asyncio.wait_for(launcher.started.wait(), timeout=5)
            second = asyncio.create_task(app.screen.choose("force-confirm"))
        else:
            first = asyncio.create_task(app.screen.choose(resolve))
            await asyncio.wait_for(launcher.started.wait(), timeout=5)
            second = asyncio.create_task(app.screen.choose(resolve))
        await asyncio.sleep(0)
        launcher.release.set()
        await asyncio.gather(first, second)

        assert launcher.issued == [expected], (
            f"two keypresses issued {launcher.issued}; exactly one {expected!r} was required"
        )


async def test_a_concurrent_second_launch_is_refused_by_the_handler_guard() -> None:
    """The `_busy` guard at `on_option_list_option_selected`, exercised by concurrent delivery.

    Named for the mechanism, not for a guarantee the surface does not give. Under **queued**
    delivery — what two fast enters actually produce — this flow issues twice: the launch
    clears `_busy` in a `finally` that runs before `self.exit(...)`, and it never changes
    the position, so the second enter finds the same screen and an open guard. Verified, and
    recorded as BL-015. This test pins the concurrent path only.
    """
    launcher = _SlowLauncher()
    app = RemoteAgentsTui(_context(launcher))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await app.screen.choose("claude")
        app.screen.submit("")
        await pilot.pause()
        first = asyncio.create_task(_select(app, "launch"))
        await asyncio.wait_for(launcher.started.wait(), timeout=5)
        second = asyncio.create_task(_select(app, "launch"))
        await asyncio.sleep(0)
        launcher.release.set()
        await asyncio.gather(first, second)
        assert launcher.issued == ["launch"], (
            f"two enters on Review issued {launcher.issued}; one launch was required"
        )


async def test_a_repeated_keypress_creates_exactly_one_project() -> None:
    """A create is a filesystem mutation and an append to the shared registry."""
    creator = _SlowCreator()
    app = RemoteAgentsTui(_context(_SlowLauncher(), creator))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.show_areas()
        await app.screen.choose("infra")
        app.screen.submit("new-project")
        await pilot.pause()
        first = asyncio.create_task(_select(app, "create"))
        assert await asyncio.to_thread(creator.started.wait, 5), "the create never started"
        second = asyncio.create_task(_select(app, "create"))
        await asyncio.sleep(0)
        creator.release.set()
        await asyncio.gather(first, second)
        assert creator.issued == ["create"], (
            f"two enters on Review issued {creator.issued}; one create was required"
        )


async def test_the_guard_is_the_reason_and_not_a_coincidence() -> None:
    """Guards the tests above from passing for the wrong reason.

    If the flows were fast enough that a second press always landed after the first
    finished, every assertion above would hold for a surface with no protection at all. So
    this asserts the guard is actually consulted: with it already set, a stop issues nothing.

    An earlier version proved the same point by replacing `_busy` with a class-level
    descriptor that always read False. That worked, and it also mutated `RemoteAgentsTui`
    itself — state shared by every other test in the run — which made the whole directory
    fail intermittently under `-W error::RuntimeWarning`, landing on whichever unrelated test
    the interpreter happened to be in. Reaching for a class when an instance will do is not
    worth a flaky suite.
    """
    launcher = _SlowLauncher()
    app = RemoteAgentsTui(_context(launcher))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.show_sessions()
        await app.show_detail(str(_SESSION_ID))
        await pilot.pause()

        launcher.release.set()  # this test needs the stop to run to completion
        app._busy = True
        await app.screen.choose("graceful")
        assert launcher.issued == [], (
            f"a stop was issued while the guard was set: {launcher.issued}. The guard is not "
            f"consulted, so the single-issue tests above prove nothing."
        )

        app._busy = False
        await app.screen.choose("graceful")
        assert launcher.issued == ["graceful"], (
            f"clearing the guard should let exactly one stop through; got {launcher.issued}"
        )


async def test_a_worker_does_not_outlive_the_app() -> None:
    """Task 2.1's property: an app-owned worker is cancelled when the app goes away.

    The first version of this test asserted the same thing and proved nothing. It awaited
    `action_refresh()` to completion against an instant fake, so `app.workers` was already
    empty when `exit()` was called and the assertion iterated an empty collection — it passed
    with `_in_thread` reverted to `asyncio.to_thread`, i.e. with the feature entirely absent.
    Two independent reviewers caught it; it was written immediately after another test in
    this directory whose whole purpose is to stop a suite being green while proving nothing.

    Writing it properly also corrected what the guarantee actually is. `exit()` is not
    synchronous: the worker is still RUNNING immediately after it returns, and is cancelled
    during teardown. So the reference is captured while the worker is in flight and its final
    state is asserted after the app has gone — which is the real claim, "a worker does not
    outlive the app", rather than "exit cancels it".
    """
    catalogue = _SlowCatalogue()
    launcher = _SlowLauncher()
    launcher.release.set()
    app = RemoteAgentsTui(replace(_context(launcher), refresh_catalogue=catalogue))
    in_flight: list = []
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            refreshing = asyncio.create_task(app.action_refresh())
            assert await asyncio.to_thread(catalogue.started.wait, 5), "the read never started"

            in_flight = [w for w in app.workers if w.state is WorkerState.RUNNING]
            assert in_flight, (
                "no worker was RUNNING when the app was told to quit, so this test would pass "
                "with no workers at all — which is exactly how its first version was vacuous"
            )
            app.exit(None)
            catalogue.release.set()
            with contextlib.suppress(BaseException):
                await refreshing
    finally:
        catalogue.release.set()

    assert not list(app.workers), (
        f"the app still tracks workers after shutdown: {list(app.workers)}"
    )
    assert all(w.state is WorkerState.CANCELLED for w in in_flight), (
        f"a worker outlived the app rather than being cancelled: "
        f"{[(w.group, w.state.name) for w in in_flight]}"
    )


async def test_a_cancelled_read_does_not_render_an_error_into_a_dying_screen() -> None:
    """A cancelled worker means the app is quitting, not that the read failed.

    `Worker.wait()` raises `WorkerCancelled`, which is an ordinary `Exception` — so before
    `_in_thread` translated it, the callers' `except Exception` caught it and tried to draw
    an error into a screen being torn down, raising `MountError` out of the recovery path.
    `CancelledError` is a `BaseException` and passes through untouched, which is what the
    plain await these calls replaced already did.
    """
    creator = _SlowCreator()
    app = RemoteAgentsTui(_context(_SlowLauncher(), creator))
    raised: list[str] = []
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.show_areas()
            await app.screen.choose("infra")
            app.screen.submit("new-project")
            await settle(app, pilot)
            creating = asyncio.create_task(_select(app, "create"))
            assert await asyncio.to_thread(creator.started.wait, 5), "the create never started"
            app.exit(None)
            creator.release.set()
            try:
                await creating
            except BaseException as error:  # noqa: BLE001 - the type is the assertion
                raised.append(type(error).__name__)
    finally:
        creator.release.set()
    assert raised == ["CancelledError"], (
        f"a cancelled create surfaced as {raised}; MountError here means the error path tried "
        f"to draw into a torn-down screen"
    )


async def test_the_step_is_unchanged_by_a_dropped_keypress() -> None:
    """A dropped second press must not half-navigate — the screen belongs to the first."""
    launcher = _SlowLauncher()
    app = RemoteAgentsTui(_context(launcher))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _drive_to_force_confirm(app)
        await pilot.pause()
        first = asyncio.create_task(app.screen.choose("force-confirm"))
        await asyncio.wait_for(launcher.started.wait(), timeout=5)
        assert position(app) == "FORCE_CONFIRM"
        # Bounded on purpose. This await is the *dropped* second press, so it must return
        # promptly; if a regression lets it through to the launcher it would block on
        # `release` instead, and an unbounded await would hang the run rather than fail it.
        await asyncio.wait_for(app.screen.choose("force-confirm"), timeout=5)
        launcher.release.set()
        await first
        assert launcher.issued == ["force"]


@dataclass(slots=True)
class _AdvancingLauncher:
    """A store whose record advances state, the way the real one does after a stop.

    `_SlowLauncher` returns a frozen RUNNING record, which makes `_busy` look like the thing
    protecting a repeated keypress. It is not. `_stop` re-reads the record and re-checks the
    policy at issue time — DEC-007's actual stated mitigation — and against a store that
    moves, that is what refuses the second stop.
    """

    issued: list[str] = field(default_factory=list)
    state: SessionState = SessionState.RUNNING

    async def refresh_readiness(self):
        return (_record(self.state),)

    async def list_sessions(self):
        return (_record(self.state),)

    async def copy_attach(self, _session_id):
        return None

    async def graceful_stop(self, _command):
        self.issued.append("graceful")
        # PRESERVED, not ENDED, and the distinction is the whole point: ENDED is filtered
        # out of the list entirely, so the second enter would be refused merely because the
        # record could not be found. PRESERVED stays listed and still offers actions — just
        # not `graceful` — so what refuses the second enter is the policy re-check itself.
        self.state = SessionState.PRESERVED

    async def cleanup(self, _command):
        self.issued.append("cleanup")
        self.state = SessionState.ENDED

    async def force_stop(self, _command):
        self.issued.append("force")
        self.state = SessionState.ENDED


async def test_two_queued_enters_issue_one_stop_through_the_real_delivery_path() -> None:
    """The delivery model the other tests do not exercise, against a store that moves.

    A real repeated keypress is not two concurrent coroutines: `OptionList` *posts*
    `OptionSelected`,
    and Textual serialises handlers on the app's message pump, so the second is dispatched
    only after the first handler has returned and `_busy` has already been cleared. The
    concurrent-task tests above therefore pin a shape the framework never produces.

    Posted this way against a frozen-record fake, two Enters really do issue two gracefuls.
    Against a store whose state advances — which is what the real one does — exactly one is
    issued, refused by `_stop`'s record re-read and policy re-check. That is the guarantee
    DEC-007 actually rests on, so this is the test that pins it.

    Verified by mutation rather than assumed: disabling the policy re-check in `_stop` makes
    this fail. An earlier version advanced the record to ENDED instead, which is filtered out
    of the list altogether — so the second enter was refused for merely not finding the
    record, and the test passed with the re-check removed, attributing the protection to a
    mechanism it was not exercising.
    """
    launcher = _AdvancingLauncher()
    app = RemoteAgentsTui(_context(launcher))  # type: ignore[arg-type]
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.show_sessions()
        await app.show_detail(str(_SESSION_ID))
        await settle(app, pilot)

        choices = app.screen.query_one("#choices", OptionList)
        index = choices.get_option_index("graceful")
        chosen = choices.get_option_at_index(index)
        # Posted twice with nothing awaited between them: two fast enters, exactly.
        # To the *screen*, because that is where the handler lives after the extraction and
        # where a bubbling `OptionSelected` is now consumed — posting to the app would queue
        # two messages nothing handles, and the test would pass by issuing nothing at all.
        app.screen.post_message(OptionList.OptionSelected(choices, chosen, index))
        app.screen.post_message(OptionList.OptionSelected(choices, chosen, index))
        await settle(app, pilot)
        await pilot.pause()

        assert launcher.issued == ["graceful"], (
            f"two queued enters issued {launcher.issued}; the record re-read should have "
            f"refused the second once the session left RUNNING"
        )


async def test_a_concurrent_second_remote_control_change_is_refused() -> None:
    """One of the two flows Task 2.3 scoped and the first version never exercised.

    Remote control also survives *queued* delivery, unlike launch and resume, because
    the remote-control change returns to the session detail before finishing — so the
    second enter lands on a screen where that key means nothing. This test covers the
    concurrent path; the step change covers the other.

    Routed through `_select` rather than the resolver, because the remote-control change —
    like the launch and the project creation — only *sets* the busy flag and never reads it.
    An earlier attempt asserted against the resolver directly and failed for exactly that
    reason, which is the same lesson `_select`'s own docstring records.
    """
    launcher = _SlowLauncher()
    app = RemoteAgentsTui(_context(launcher))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.show_sessions()
        await app.show_detail(str(_SESSION_ID))
        await app.screen.confirm_remote_control()
        await settle(app, pilot)

        first = asyncio.create_task(_select(app, "remote-control-active"))
        await asyncio.wait_for(launcher.started.wait(), timeout=5)
        second = asyncio.create_task(_select(app, "remote-control-active"))
        await asyncio.sleep(0)
        launcher.release.set()
        await asyncio.gather(first, second)

        assert launcher.issued == ["remote-control"], (
            f"two enters issued {launcher.issued}; exactly one change was required"
        )


async def test_a_concurrent_second_resume_is_refused_by_the_handler_guard() -> None:
    """The other flow Task 2.3 scoped, on the concurrent path only.

    Same caveat as launch, and for the same structural reason: resume ends in `self.exit(...)`
    without leaving the position, so queued delivery issues twice (BL-015). An earlier version of
    this test was additionally *named* for the queued model while using concurrent tasks.

    The conversation is a real `ResolvedConversation`, not a stand-in. An earlier version
    passed a bare `object()`, which made `ResumeCommand.__post_init__` raise before the
    service was ever reached — so it recorded "no command issued" and would have passed with
    the guard removed entirely. Vacuous in the same way the cancellation test was.
    """
    summary = ConversationSummary(
        ConversationReference("c-" + "0" * 14 + "01"),
        ProfileId("claude"),
        ProjectId("opaque-existing"),
        ConversationState.RESUMABLE,
        datetime.now(UTC),
        description="a saved conversation",
    )
    launcher = _SlowLauncher()
    app = RemoteAgentsTui(_context(launcher))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # The confirm is a screen of its own now, carrying the project, agent and resolved
        # conversation it was built with — so this pushes the real thing rather than setting
        # four fields on the app and painting the rows by hand. The selection handler
        # dispatches on whatever screen is on top, which is exactly what is under test.
        await app.push_screen(
            ResumeConfirmScreen(
                _PROJECT, "claude", ResolvedConversation(summary, ProviderConversationId("abc123"))
            )
        )
        await settle(app, pilot)

        first = asyncio.create_task(_select(app, "resume-confirm"))
        await asyncio.wait_for(launcher.started.wait(), timeout=5)
        second = asyncio.create_task(_select(app, "resume-confirm"))
        await asyncio.sleep(0)
        launcher.release.set()
        await asyncio.gather(first, second)

        assert launcher.issued == ["resume"], (
            f"two enters issued {launcher.issued}; exactly one resume was required"
        )
