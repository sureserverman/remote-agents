"""Every flow that awaits a command says so on the widget the owner is looking at.

Five flows issue a command and then wait on something outside this process — launch, resume,
project create, stop and Remote Control. Until this, the only sign any of them was working was
`"Launching…"` written into the status line by one of the five, and the rows staying stale
under the cursor for the other four. A surface that looks identical whether it is working or
has silently done nothing teaches the owner to press the key again.

**The affordance is on `#choices`, not on the screen**, and that is the point rather than an
implementation detail: what the owner must not act on while a command is in flight is exactly
the row list, and covering it is what says so. The status line underneath keeps saying
something true meanwhile — the flow's own verb, not the instruction it was carrying before.

**Each flow is named literally below rather than derived from the code.** A parametrization
over "every method that takes the guard" would agree with whatever the code happens to do and
could not fail when a sixth flow forgets the affordance — the same trap the Stage 1 handoff
recorded twice, once for `work_in_flight` and once for the snapshot suite's `_POSITIONS`.

Every case drives the flow to a *gate* the test holds open, so "while the worker runs" is a
window the test controls rather than a race it hopes to win. Both outcomes are asserted for
every flow, because a `finally` that clears on success and a `finally` that clears at all are
different claims and only one of them survives an early `return`.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from backends import backend_for
from stop_results import a_clean_stop, a_verified_force_stop
from textual.widgets import OptionList
from tui_feedback import working

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.project_admin import CreatedProject, CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.services import ResumeOutcome
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProfileResumeCapability,
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
from remote_agents.ports.terminal import TerminalObservation

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_SESSION_ID = SessionId.new()
_REFERENCE = ConversationReference("c-0000000000000001")


def _record(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        _SESSION_ID,
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        state,
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


class _Gate:
    """One command held open, then released into a success or a failure.

    An event the test sets rather than a sleep: the assertion "the widget is loading while the
    worker runs" is only worth making if the window is the test's to open and close, and a
    timing-based version of it passes on a fast machine whether or not the affordance exists.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.opened = threading.Event()
        # A `threading.Event` rather than an `asyncio` one because `run_blocking` waits on it
        # from a real worker thread, where `await` is not available and a spin loop would
        # contend for the GIL with the pump it is waiting on — a plausible source of CI
        # flakiness in a test whose whole point is a window it controls. `set()` is safe to
        # call from the event-loop thread, and `run`'s async form polls it through the pump.
        self.release = threading.Event()
        self.error = error

    async def run(self):
        self.opened.set()
        while not self.release.is_set():
            await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return None

    def run_blocking(self):
        """The synchronous form, for the flow that goes through `in_thread`."""
        self.opened.set()
        self.release.wait(timeout=10)
        if self.error is not None:
            raise self.error
        return None


@dataclass(slots=True)
class _Launcher:
    """Every service call the five flows make, each able to block on the test's gate."""

    gate: _Gate | None = None
    records: tuple[SessionRecord, ...] = field(default_factory=lambda: (_record(),))
    state: SessionState = SessionState.RUNNING

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        return None

    async def launch(self, _command) -> SessionRecord:
        await self._gated()
        return _record(self.state)

    async def resume(self, _command) -> ResumeOutcome:
        await self._gated()
        return ResumeOutcome(_record(self.state), created=True)

    async def graceful_stop(self, _command) -> TerminalObservation:
        await self._gated()
        return a_clean_stop()

    async def cleanup(self, _command) -> None:
        await self._gated()

    async def force_stop(self, _command):
        await self._gated()
        return a_verified_force_stop()

    async def set_remote_control(self, _command) -> RemoteControlState:
        await self._gated()
        return RemoteControlState.ACTIVE

    async def _gated(self) -> None:
        if self.gate is not None:
            await self.gate.run()


class _Creator:
    def __init__(self, gate: _Gate | None = None) -> None:
        self.gate = gate

    def available_areas(self) -> tuple[str, ...]:
        return ("infra",)

    def create(self, command: CreateProjectCommand) -> CreatedProject:
        if self.gate is not None:
            self.gate.run_blocking()
        identity = ProjectIdentity(area=command.area, name=command.name)
        return CreatedProject(identity, Path("/dev") / command.area / command.name)


class _Conversations:
    async def capabilities(self) -> tuple[ProfileResumeCapability, ...]:
        return (ProfileResumeCapability(ProfileId("claude"), True, True),)

    async def catalogue(self, query) -> ConversationCataloguePage:
        return ConversationCataloguePage((_summary(),), query.page, 1)

    async def resolve_for_resume(self, reference: ConversationReference):
        if reference != _REFERENCE:
            return None
        return ResolvedConversation(_summary(), ProviderConversationId("abc123"))


def _context(launcher: _Launcher, creator: _Creator | None = None) -> TuiContext:
    return TuiContext(
        backend=backend_for(
            sessions=launcher,  # type: ignore[arg-type]
            projects=creator or _Creator(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_PROJECT,),
            catalogue=(_PROJECT,),
            conversations=_Conversations(),  # type: ignore[arg-type]
        ),
        profiles=(ProfileChoice("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


async def _walk_to_review(app: RemoteAgentsTui, pilot) -> None:
    await app.screen.choose("opaque-existing")
    await pilot.pause()
    await app.screen.choose("launch")
    await pilot.pause()
    await app.screen.choose("claude")
    await pilot.pause()


async def _walk_to_the_conversation_list(app: RemoteAgentsTui, pilot) -> None:
    """Stop on the list: choosing a row there is the resume, not a step toward it."""
    await app.action_resume()
    await pilot.pause()
    await app.screen.choose("opaque-existing")
    await pilot.pause()
    await app.screen.choose("claude")
    await pilot.pause()


async def _walk_to_new_project_review(app: RemoteAgentsTui, pilot) -> None:
    await app.action_add_project()
    await pilot.pause()
    await app.screen.choose("infra")
    await pilot.pause()
    app.screen.submit("brand-new")
    await pilot.pause()


# One arrangement per flow. Each opens the gate by issuing the command, and the test drives
# it the rest of the way; `walk` leaves the surface on the position the command is issued from.
_FLOWS = {
    "launch": (_walk_to_review, "launch"),
    "resume": (_walk_to_the_conversation_list, str(_REFERENCE)),
    "project-create": (_walk_to_new_project_review, "create"),
    "stop": (None, "graceful"),
    "remote-control": (None, "remote-control-active"),
}


async def _issue(app: RemoteAgentsTui, pilot, flow: str) -> asyncio.Task:
    """Start `flow`'s command and hand back the task still waiting on the gate.

    Remote Control is the one flow that asks before it acts, so its command is not issued by
    the row selection at all — the modal has to be answered first. Walking it that way rather
    than reaching past the modal is what keeps this asserting about the *flow* an owner drives
    rather than about a method call no keypress can produce.
    """
    walk, key = _FLOWS[flow]
    if walk is None:
        await app.show_detail(str(_SESSION_ID))
        await pilot.pause()
    else:
        await walk(app, pilot)
    screen = app.screen
    issued = asyncio.create_task(screen.choose(key))
    await pilot.pause()
    if flow == "remote-control":
        await _answer_the_modal(pilot, app)
    return issued


async def _answer_the_modal(pilot, app: RemoteAgentsTui) -> None:
    """Move onto the confirm row and answer yes, however many rows down it is."""
    choices = app.screen.query_one("#choices", OptionList)
    target = [option.id for option in choices.options].index(app.screen.confirm_key)
    for _ in range(target):
        await pilot.press("down")
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.parametrize("flow", sorted(_FLOWS))
async def test_the_rows_report_working_while_the_command_is_in_flight(flow: str) -> None:
    gate = _Gate()
    launcher = _Launcher(gate=gate)
    app = RemoteAgentsTui(_context(launcher, _Creator(gate)))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        issued = await _issue(app, pilot, flow)
        await asyncio.wait_for(_opened(gate, pilot), timeout=5)
        during = working(app)

        gate.release.set()
        await asyncio.wait_for(issued, timeout=5)
        await pilot.pause()
        after = working(app)

    assert during is True, f"{flow} awaited a command with no sign it was working"
    assert after is False, f"{flow} left the rows covered after the command landed"


@pytest.mark.parametrize("flow", sorted(_FLOWS))
async def test_a_command_that_raises_still_uncovers_the_rows(flow: str) -> None:
    """The `finally`, which is the half a success-only test cannot distinguish.

    Every one of these five reports its failure and returns early, so a clear written on the
    success path alone would leave the position permanently covered by the affordance — the
    surface stuck looking busy after the thing it was busy with has already failed.
    """
    gate = _Gate(error=RuntimeError("the terminal port broke its contract"))
    launcher = _Launcher(gate=gate)
    app = RemoteAgentsTui(_context(launcher, _Creator(gate)))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        issued = await _issue(app, pilot, flow)
        await asyncio.wait_for(_opened(gate, pilot), timeout=5)
        during = working(app)

        gate.release.set()
        await asyncio.wait_for(issued, timeout=5)
        await pilot.pause()
        after = working(app)

    assert during is True
    assert after is False, f"{flow} stayed covered after its command failed"


async def test_the_affordance_covers_the_rows_and_not_the_status_line() -> None:
    """What is unsafe to act on is the row list; what is still worth reading is the status.

    Asserted because "show a loading affordance" has an obvious wrong reading — covering the
    whole screen — and taking it would hide the one line that says what the surface is doing.
    """
    gate = _Gate()
    app = RemoteAgentsTui(_context(_Launcher(gate=gate)))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        issued = await _issue(app, pilot, "launch")
        await asyncio.wait_for(_opened(gate, pilot), timeout=5)
        rows_covered = app.screen.query_one("#choices", OptionList).loading
        status_readable = str(app.screen.query_one("#status").content)

        gate.release.set()
        await asyncio.wait_for(issued, timeout=5)

    assert rows_covered is True
    assert status_readable, "the status line was blank while the surface was working"


@pytest.mark.parametrize("flow", sorted(_FLOWS))
async def test_every_flow_says_what_it_is_doing_while_it_does_it(flow: str) -> None:
    """A line that was true a moment ago and is false now is worse than no line at all.

    The first version of this affordance covered the rows and left the status alone, arguing
    it stayed readable underneath. What `ReviewScreen` was left saying is "Label: none. Launch,
    or go back." — an instruction to press a button that is at that moment covered and refusing
    input. A review caught it; this is what stops it coming back.

    Asserted through the trailing ellipsis rather than by matching each flow's wording, which
    would copy the five strings into this file and make it fail on a rewording rather than on
    a regression. No position's resting instruction ends in one — they end in a full stop or a
    question mark — so it is a property of "this is in progress" and not of any one message.
    """
    gate = _Gate()
    launcher = _Launcher(gate=gate)
    app = RemoteAgentsTui(_context(launcher, _Creator(gate)))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        issued = await _issue(app, pilot, flow)
        await asyncio.wait_for(_opened(gate, pilot), timeout=5)
        during = str(app.screen.query_one("#status").content)

        gate.release.set()
        await asyncio.wait_for(issued, timeout=5)
        await pilot.pause()

    assert during.endswith("…"), f"{flow} covered the rows and said {during!r} while it worked"


async def test_the_line_the_position_had_is_put_back_when_the_command_lands() -> None:
    """The other half of `awaiting`'s contract, and the only place it can be seen alone.

    None of the five flows can show it: every one of them re-renders or exits the app once its
    command lands, so what the owner ends up reading is the *result* rather than the restored
    instruction. That is exactly why the restore is worth pinning here — it is invisible until
    a sixth flow forgets to re-render, and then it is "Stopping…" left on screen forever.
    """
    app = RemoteAgentsTui(_context(_Launcher()))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        resting = str(app.screen.query_one("#status").content)
        async with app.screen.awaiting("Doing the thing…"):
            await pilot.pause()
            during = str(app.screen.query_one("#status").content)
        await pilot.pause()
        after = str(app.screen.query_one("#status").content)

    assert during == "Doing the thing…"
    assert after == resting, "the position did not get its own line back"


async def _opened(gate: _Gate, pilot, *, passes: int = 500) -> None:
    """Wait for the command to reach the gate, pumping the surface while it gets there.

    Bounded by its own pass count as well as by the caller's `wait_for`, so it is safe to
    reuse without remembering to wrap it — an unbounded pump loop in a test helper is a hang
    with no message rather than a failure with one.
    """
    for _ in range(passes):
        if gate.opened.is_set():
            return
        await pilot.pause()
    raise AssertionError("the command never reached the gate")
