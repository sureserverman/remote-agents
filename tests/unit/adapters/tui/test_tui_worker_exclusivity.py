"""A repeated keypress issues one command, and quitting leaves nothing running.

Two guarantees this project relied on and had never tested.

The first is DEC-007's: a second enter arriving while a mutation is in flight must be
**dropped**, not queued and not allowed to cancel and restart the one already running. On a
force stop the difference is not academic — cancel-and-restart means the profile's exit
sequence has already reached the pane, the operation is abandoned mid-kill, and a second kill
is issued. That is exactly what DEC-008 records, and it is why the guard is a flag rather than
Textual's `exclusive=True`, which measurably does the cancelling thing.

The second is the property Task 2.1 bought by moving the blocking calls onto app-owned
workers: quitting while one is in flight must leave no thread running and no coroutine
unawaited, rather than a thread writing into a torn-down screen.

Neither is asserted anywhere else, so a refactor could quietly remove either. `test_tui_force_stop.py`
covers the *cursor* half of DEC-007's mitigation — that no screen rests on a mutating row — and
this file covers the *concurrency* half.
"""

from __future__ import annotations

import asyncio
import threading
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from remote_agents.adapters.tui.app import RemoteAgentsTui, Step
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.project_admin import CreatedProject
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
    itself (app.py:986), but `_launch` and `_resolve_project_review` only *set* it and rely
    on `on_list_view_selected` (app.py:391) to have refused the second event. Calling those
    resolvers directly therefore walks straight past the protection and issues two commands.

    So these tests go through the handler. The uneven placement is worth knowing about on its
    own: the screen rewrite replaces that dispatch, and a version that forgets the caller-side
    check would leave launch and create unguarded while the stops stayed safe.
    """
    from textual.widgets import ListItem, ListView

    choices = app.query_one("#choices", ListView)
    rows = list(app.query(ListItem))
    index = next(i for i, row in enumerate(rows) if getattr(row, "entry_key", None) == key)
    await app.on_list_view_selected(ListView.Selected(choices, rows[index], index))


async def _drive_to_force_confirm(app: RemoteAgentsTui) -> None:
    await app._show_sessions()
    await app._show_detail(str(_SESSION_ID))
    await app._confirm_force()


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
        await app._show_sessions()
        await app._show_detail(str(_SESSION_ID))
        await pilot.pause()

        if resolve == "force-confirm":
            await app._confirm_force()
            await pilot.pause()
            first = asyncio.create_task(app._resolve_force_confirm("force-confirm"))
            await launcher.started.wait()
            second = asyncio.create_task(app._resolve_force_confirm("force-confirm"))
        else:
            first = asyncio.create_task(app._resolve_detail(resolve))
            await launcher.started.wait()
            second = asyncio.create_task(app._resolve_detail(resolve))
        await asyncio.sleep(0)
        launcher.release.set()
        await asyncio.gather(first, second)

        assert launcher.issued == [expected], (
            f"two keypresses issued {launcher.issued}; exactly one {expected!r} was required"
        )


async def test_a_repeated_keypress_issues_exactly_one_launch() -> None:
    """The same guarantee on the flow that creates work rather than destroying it."""
    launcher = _SlowLauncher()
    app = RemoteAgentsTui(_context(launcher))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._choose_project("opaque-existing")
        app._choose_profile("claude")
        app._submit_label("")
        await pilot.pause()
        first = asyncio.create_task(_select(app, "launch"))
        await launcher.started.wait()
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
        await app._show_areas()
        await app._choose_area("infra")
        app._submit_name("new-project")
        await pilot.pause()
        first = asyncio.create_task(_select(app, "create"))
        await asyncio.to_thread(creator.started.wait, 5)
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
        await app._show_sessions()
        await app._show_detail(str(_SESSION_ID))
        await pilot.pause()

        launcher.release.set()  # this test needs the stop to run to completion
        app._busy = True
        await app._resolve_detail("graceful")
        assert launcher.issued == [], (
            f"a stop was issued while the guard was set: {launcher.issued}. The guard is not "
            f"consulted, so the single-issue tests above prove nothing."
        )

        app._busy = False
        await app._resolve_detail("graceful")
        assert launcher.issued == ["graceful"], (
            f"clearing the guard should let exactly one stop through; got {launcher.issued}"
        )


async def test_quitting_mid_launch_leaves_no_worker_running() -> None:
    """Task 2.1's property: an app-owned worker is cancelled when the app goes away."""
    launcher = _SlowLauncher()
    app = RemoteAgentsTui(_context(launcher))
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.action_refresh()
            await pilot.pause()
            app.exit(None)
        assert not [worker for worker in app.workers if worker.is_running], (
            f"workers still running after exit: {list(app.workers)}"
        )


async def test_the_step_is_unchanged_by_a_dropped_keypress() -> None:
    """A dropped second press must not half-navigate — the screen belongs to the first."""
    launcher = _SlowLauncher()
    app = RemoteAgentsTui(_context(launcher))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _drive_to_force_confirm(app)
        await pilot.pause()
        first = asyncio.create_task(app._resolve_force_confirm("force-confirm"))
        await launcher.started.wait()
        assert app._step is Step.FORCE_CONFIRM
        await app._resolve_force_confirm("force-confirm")
        launcher.release.set()
        await first
        assert launcher.issued == ["force"]
