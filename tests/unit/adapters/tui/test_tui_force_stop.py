"""Force stop is irreversible, so it takes two deliberate choices and defaults to abort.

The second choice is a **modal** now, and that changes how these tests drive it. Choosing
Force no longer returns once a confirmation screen has been pushed: `confirm_force` awaits the
modal's answer and only then decides whether to issue anything. That is the property the stage
buys — the caller that asked the question is the one that learns the answer, so there is no
longer a path where the question is abandoned and the surface simply carries on — and it is
why every test here runs the choice as a task, answers the modal with real keys, and joins.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from backends import backend_for
from stop_results import a_clean_stop, a_verified_force_stop
from textual.widgets import OptionList
from tui_feedback import status as _status
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.model import _BACK
from remote_agents.adapters.tui.screens.confirm import ConfirmScreen
from remote_agents.application.commands import ForceStopCommand
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.ports.terminal import TerminalObservation

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")


def _record(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _RecordingLauncher:
    records: tuple[SessionRecord, ...] = ()
    issued: list[object] = field(default_factory=list)
    #: What `force_stop` observed. Defaults to the kill that worked; a test wanting BL-026's
    #: case swaps in `a_force_stop_that_found_nothing`. A field rather than a flag because the
    #: surface reads the whole observation, and a bool here would decide for it.
    force_result: Callable[[], TerminalObservation] = a_verified_force_stop

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        return None

    async def force_stop(self, command: ForceStopCommand):
        self.issued.append(command)
        return self.force_result()

    async def graceful_stop(self, command):
        self.issued.append(command)
        return a_clean_stop()

    async def cleanup(self, command) -> None:
        self.issued.append(command)


def _context(launcher: _RecordingLauncher) -> TuiContext:
    return TuiContext(
        backend=backend_for(
            sessions=launcher,  # type: ignore[arg-type]
            projects=object(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_PROJECT,),
            catalogue=(_PROJECT,),
        ),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


def _rows(app: RemoteAgentsTui) -> list[str]:
    return [str(option.prompt) for option in app.screen.query_one("#choices", OptionList).options]


def _keys(app: RemoteAgentsTui) -> list[str]:
    return [option.id for option in app.screen.query_one("#choices", OptionList).options]


async def _open_the_confirm(app: RemoteAgentsTui, pilot) -> asyncio.Task[None]:
    """Choose Force on the detail and leave the modal open, as a keypress would.

    The choice cannot be awaited: `confirm_force` does not return until the question has been
    answered. So it runs as a task the test answers with keys and then joins — which is also
    the closest a test gets to the real arrangement, where the keypress handler is likewise
    suspended for exactly as long as the owner takes to decide.
    """
    task = asyncio.create_task(app.screen.choose("force"))
    await pilot.pause()
    return task


async def _answered(task: asyncio.Task[None]) -> None:
    """Join the suspended choice, bounded so a modal that never resolves fails rather than hangs."""
    await asyncio.wait_for(task, timeout=5)


async def test_choosing_force_opens_a_confirm_modal_and_issues_nothing_yet() -> None:
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot)
        step = position(app)
        status = _status(app)
        modal = app.screen.is_modal
        await pilot.press("escape")
        await _answered(asking)

    assert step == "FORCE_MODAL"
    assert modal, "the confirmation must be modal, or an app binding can leave it unanswered"
    assert launcher.issued == [], "force must not be issued on the first selection"
    assert record.display.rendered in status, "the confirm step must name the session"


async def test_the_confirm_modal_opens_with_abort_highlighted() -> None:
    """A stray enter must abort, not destroy — the wizard's review-before-mutate rule."""
    record = _record()
    app = RemoteAgentsTui(_context(_RecordingLauncher((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot)
        highlighted = app.screen.query_one("#choices").highlighted
        keys = _keys(app)
        await pilot.press("escape")
        await _answered(asking)

    assert keys[highlighted] != "force-confirm", "the destructive option must not be preselected"


async def test_a_single_stray_enter_at_the_confirm_modal_destroys_nothing() -> None:
    """The plan's named case: a bare Enter on the freshly-pushed modal issues zero commands."""
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot)
        await pilot.press("enter")
        await _answered(asking)
        await pilot.pause()
        step = position(app)

    assert launcher.issued == []
    assert step == "SESSION_DETAIL", "an aborted confirmation must leave the owner on the detail"


async def test_escape_at_the_confirm_modal_aborts_and_issues_nothing() -> None:
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot)
        await pilot.press("escape")
        await _answered(asking)
        await pilot.pause()
        step = position(app)

    assert launcher.issued == []
    assert step == "SESSION_DETAIL"


async def test_only_the_second_confirmation_issues_the_force_stop() -> None:
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot)
        assert launcher.issued == []
        # Moving off the abort is the second deliberate act; nothing else issues.
        await pilot.press("down")
        await pilot.press("enter")
        await _answered(asking)

    assert len(launcher.issued) == 1
    assert isinstance(launcher.issued[0], ForceStopCommand)
    assert launcher.issued[0].session_id == record.session_id


async def test_the_confirm_modal_says_the_action_is_irreversible() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_RecordingLauncher((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot)
        status = _status(app).casefold()
        await pilot.press("escape")
        await _answered(asking)

    assert "cannot be undone" in status or "irreversible" in status


async def test_aborting_returns_to_a_detail_that_still_offers_force() -> None:
    """Abort is not a dead end; the owner may have simply wanted to re-read the state."""
    record = _record()
    app = RemoteAgentsTui(_context(_RecordingLauncher((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot)
        await pilot.press("enter")  # the abort, since that is where the cursor rests
        await _answered(asking)
        await pilot.pause()
        keys = _keys(app)

    assert "force" in keys


async def test_a_session_that_vanished_before_confirming_is_not_forced() -> None:
    """DEC-007's fourth mitigation, and the modal is what makes its window worth having.

    The owner can leave the question open for as long as they like, so the record read to
    build it is exactly the one most likely to be stale by the time it is answered. `stop`
    re-reads and re-checks rather than trusting the record the modal was built from.
    """
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot)
        launcher.records = ()
        await pilot.press("down")
        await pilot.press("enter")
        await _answered(asking)
        await pilot.pause()
        status = _status(app)

    assert launcher.issued == []
    assert "no longer available" in status.casefold()


async def test_force_is_not_reachable_by_one_keypress_from_the_list() -> None:
    """From the sessions list, no single enter can destroy anything."""
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        await pilot.press("enter")  # opens detail
        await pilot.pause()
        await pilot.press("enter")  # whatever is highlighted in detail
        await pilot.pause()

    assert launcher.issued == [], "no two keystrokes from the list may issue a stop"


@pytest.mark.parametrize("presses", [1, 2, 3, 5, 10, 25])
@pytest.mark.parametrize("state", list(SessionState))
async def test_mashing_enter_from_the_sessions_list_destroys_nothing(
    presses: int, state: SessionState
) -> None:
    """The gate's judgment criterion, made executable.

    A destructive action must not be reachable by repeating one key. Every screen resets
    the highlight to index 0, and index 0 is never a mutating entry — so an owner holding
    enter walks into Copy attach and stays there, whatever the session's state. The modal
    inherits the same rule: index 0 there is the abort.
    """
    launcher = _RecordingLauncher(tuple(_record(state) for _ in range(5)))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await pilot.press("ctrl+s")
        await pilot.pause()
        for _ in range(presses):
            await pilot.press("enter")
            await pilot.pause()

    assert launcher.issued == [], f"{presses} repeated enters issued {launcher.issued}"


@pytest.mark.parametrize("state", list(SessionState))
async def test_no_screen_puts_a_mutating_entry_under_the_resting_cursor(
    state: SessionState,
) -> None:
    """Index 0 is where a stray keypress lands, so index 0 must always be harmless."""
    record = _record(state)
    app = RemoteAgentsTui(_context(_RecordingLauncher((record,))))
    mutating = {
        "graceful",
        "cleanup",
        "force",
        "remote-control-active",
        "remote-control-inactive",
        "force-confirm",
    }

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        detail_keys = _keys(app)
        assert detail_keys[app.screen.query_one("#choices").highlighted] not in mutating

        if "force" in detail_keys:
            asking = await _open_the_confirm(app, pilot)
            confirm_keys = _keys(app)
            assert confirm_keys[app.screen.query_one("#choices").highlighted] not in mutating
            await pilot.press("escape")
            await _answered(asking)


async def test_a_failed_force_does_not_leave_the_cursor_on_the_confirm_button() -> None:
    """The one same-key-repeat path that could destroy: retry after a transient failure.

    If a stop fails and the screen is left as it was, the cursor is still resting on the row
    that issued it — so a second enter re-issues the kill without a fresh decision. Under the
    modal the failure is reported onto the *detail*, because the modal has already been
    dismissed by the answer that issued the stop, and that changes what this can assert.
    `"force-confirm"` is a row that exists only on the modal, so "the cursor is not resting on
    it" is now true whatever the failure path does — an assertion that cannot fail. The
    falsifiable form is that path's actual post-condition: the detail is left showing one Back
    row, resting on it. That fails if the cursor is left anywhere a repeat enter could act.
    """

    @dataclass(slots=True)
    class _FailingLauncher:
        records: tuple[SessionRecord, ...] = ()
        issued: list[object] = field(default_factory=list)

        async def refresh_readiness(self):
            return self.records

        async def list_sessions(self):
            return self.records

        async def copy_attach(self, _session_id):
            return None

        async def force_stop(self, command):
            self.issued.append(command)
            raise RuntimeError("tmux server is gone")

        async def graceful_stop(self, command):
            self.issued.append(command)
            raise RuntimeError("tmux server is gone")

        async def cleanup(self, command) -> None:
            self.issued.append(command)

    record = _record()
    launcher = _FailingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot)
        # Navigate the way an owner does: the cursor ends up ON the confirm row.
        await pilot.press("down")
        await pilot.press("enter")
        await _answered(asking)
        await pilot.pause()
        assert len(launcher.issued) == 1

        keys = _keys(app)
        resting = keys[app.screen.query_one("#choices").highlighted] if keys else None
        # Whatever the owner's next enter lands on, it must not be another kill.
        await pilot.press("enter")
        await pilot.pause()

    assert keys == [_BACK], f"a failed force left the detail offering {keys}"
    assert resting == _BACK, "the cursor must be moved off every row that acts"
    assert len(launcher.issued) == 1, "a repeated enter re-issued the force stop"


async def _answer_any_open_modal(app: RemoteAgentsTui, pilot) -> None:
    """Abort a confirmation if one is showing, so a suspended caller can finish."""
    if isinstance(app.screen, ConfirmScreen):
        await pilot.press("escape")
        await pilot.pause()


# Every entry point on the session detail that reads the store and then draws or pushes.
# Named as a set rather than as the two that were reported: this defect was found once in
# `confirm_force`/`confirm_remote_control`, fixed there, and then found again in
# `show_attach`/`show_inspect` — which were written in the same task and missed because the
# guard was a per-method opt-in. The parametrization is what makes the *class* covered, so a
# fifth reader added later fails here rather than being discovered by whoever hits the crash.
#
# Each entry is name → how to call it, because the Remote Control read takes the direction the
# detail row chose. A bare name list would have quietly dropped that one when it gained the
# argument, which is the same "sweep with a hole" this parametrization exists to prevent.
_DETAIL_READS: dict[str, Callable[[object], Awaitable[None]]] = {
    "confirm_force": lambda detail: detail.confirm_force(),
    "confirm_remote_control": lambda detail: detail.confirm_remote_control(
        RemoteControlState.ACTIVE
    ),
    "show_attach": lambda detail: detail.show_attach(),
    "show_inspect": lambda detail: detail.show_inspect(),
}


@pytest.mark.parametrize("entry_point", _DETAIL_READS)
async def test_escape_during_a_detail_read_neither_crashes_nor_detaches(
    entry_point: str,
) -> None:
    """Escape fired while the detail is mid-read must not strand, detach, or take the app down.

    `action_back` runs on the app's message pump while these reads run on the screen's — two
    tasks, genuinely interleaved. Two distinct failures follow from an unguarded read, and
    both were live regressions found in review rather than hypotheticals:

    * the coroutine resumes and *pushes* onto whatever the Escape revealed, so a
      "Yes, force stop it" dialog — or an output pane — ends up above a position that is not
      showing the session it names;
    * or it resumes and *draws* onto a screen whose widgets are already gone. Textual raises
      `NoMatches` there, and an exception out of a message handler exits the whole app — from
      the very paths that exist to report a vanished session without losing it.

    The second is why `_DETAIL_READS` includes the two read-only entry points and not just the
    destructive ones: `show_attach` cannot issue anything, and could still crash the surface.

    The stack is inspected *before* the confirmation is answered, deliberately: with the modal
    suspended on top is exactly when a push onto the wrong position would be visible, and
    answering first would tidy the evidence away.
    """
    record = _record()

    @dataclass(slots=True)
    class _SlowReads:
        records: tuple[SessionRecord, ...] = ()
        issued: list[object] = field(default_factory=list)

        async def refresh_readiness(self):
            return self.records

        async def list_sessions(self):
            # Wide enough that the Escape below lands inside the read rather than after it.
            await asyncio.sleep(0.03)
            return self.records

        async def copy_attach(self, _session_id):
            return None

        async def force_stop(self, command):
            self.issued.append(command)
            return a_verified_force_stop()

        async def set_remote_control(self, command):
            self.issued.append(command)
            return RemoteControlState.ACTIVE

    launcher = _SlowReads((record,))
    app = RemoteAgentsTui(_context(launcher))  # type: ignore[arg-type]

    async def _escape_during() -> None:
        await asyncio.sleep(0.005)
        await app.action_back()

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        detail = app.screen

        reading = asyncio.create_task(_DETAIL_READS[entry_point](detail))
        await _escape_during()
        await pilot.pause()

        # Whatever the interleave produced, the surface is coherent. Three outcomes are all
        # correct — the Escape was refused and the detail is still on top; the Escape won and
        # the push was refused, leaving the resting position; or the push landed, in which
        # case it must sit on the detail it was built from. The one outcome ruled out is a
        # screen pushed onto a position that is not that detail.
        stack = app.screen_stack
        if len(stack) > 1 and app.screen is not detail:
            assert stack[-2] is detail, (
                f"{entry_point} pushed onto {type(stack[-2]).__name__}, "
                "not the session detail it describes"
            )
        await _answer_any_open_modal(app, pilot)
        await asyncio.wait_for(reading, timeout=5)
        assert launcher.issued == [], f"{entry_point} must issue nothing"


@pytest.mark.parametrize("entry_point", _DETAIL_READS)
async def test_a_session_vanishing_during_an_escape_does_not_take_the_app_down(
    entry_point: str,
) -> None:
    """The crash half, on the branch that only runs when the store answers `None`.

    This is the case the re-read exists to catch — the other writer ended the session while
    the owner was looking at it — arriving at the same moment as an Escape. Before the fix
    the "no longer available" report was written to an unmounted screen and `NoMatches`
    escaped the handler, exiting the app.
    """
    record = _record()

    @dataclass(slots=True)
    class _Vanishing:
        seen: int = 0
        issued: list[object] = field(default_factory=list)

        async def refresh_readiness(self):
            return (record,)

        async def list_sessions(self):
            await asyncio.sleep(0.03)
            self.seen += 1
            # Present for the detail's own first render, gone by the time the entry point
            # under test re-reads it.
            return (record,) if self.seen <= 1 else ()

        async def copy_attach(self, _session_id):
            return None

        async def force_stop(self, command):
            self.issued.append(command)
            return a_verified_force_stop()

        async def set_remote_control(self, command):
            self.issued.append(command)
            return RemoteControlState.ACTIVE

    launcher = _Vanishing()
    app = RemoteAgentsTui(_context(launcher))  # type: ignore[arg-type]

    async def _escape_during() -> None:
        await asyncio.sleep(0.005)
        await app.action_back()

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        detail = app.screen

        # The assertion is that this returns at all: an unguarded write to a popped screen
        # raises out of here, and in the real app that exits it.
        reading = asyncio.create_task(_DETAIL_READS[entry_point](detail))
        await _escape_during()
        await pilot.pause()
        await _answer_any_open_modal(app, pilot)
        await asyncio.wait_for(reading, timeout=5)

        assert app.is_running, f"{entry_point} took the app down when the session vanished"
        assert launcher.issued == []
