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
from backends import SessionUseCaseDouble, backend_for
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
from remote_agents.ports.terminal import TerminalObservation

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")


def _record(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        # The opaque_id, which is what the store holds. It read the catalogue *name*
        # until Stage 1, which made the naming join a no-op here -- so the assertion
        # below ("the confirm step must name the session") was true of a record that
        # had never needed naming.
        SessionDisplayIdentity("opaque-existing", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _RecordingLauncher(SessionUseCaseDouble):
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
    # The *named* record: the surface resolves the project's opaque_id to its catalogue name
    # before anything renders, so the confirmation names the session the way the row the
    # owner pressed named it. Comparing against the stored record would assert the hash form
    # the owner never sees -- and would pass a modal that named the wrong thing.
    (named,) = with_project_names((record,), (_PROJECT,))
    assert named.display.rendered in status, "the confirm step must name the session"
    assert record.display.project_slug not in status


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
    class _FailingLauncher(SessionUseCaseDouble):
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
    class _SlowReads(SessionUseCaseDouble):
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
    class _Vanishing(SessionUseCaseDouble):
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


# Force, pressed on the list ------------------------------------------------------
#
# Ask 6, for the one stop key that asks first. `s` and `c` moved onto the list in Task 2.1;
# `f` could not follow them there until its modal could be raised from this screen's own
# handler, because DEC-025's whole protection is that a confirmation is asked from something
# running on the screen's message pump — a suspension anywhere else does not hold back the
# events that would pop the modal out from under it, and DEC-025 deliberately declined a
# timeout, so such a hang has no runtime net under it.


async def _sessions_list(app: RemoteAgentsTui, pilot, index: int = 0) -> None:
    await app.action_sessions()
    await pilot.pause()
    app.screen.query_one("#choices", OptionList).highlighted = index
    await pilot.pause()


async def _press_force_on_the_list(app: RemoteAgentsTui, pilot) -> asyncio.Task[None]:
    """Press `f` and hand back the suspended handler, as `_open_the_confirm` does for a row."""
    task = asyncio.create_task(app.screen.action_row_action("force"))
    await pilot.pause()
    return task


async def test_force_from_the_list_raises_the_modal_over_the_list() -> None:
    """`f` asks, and asks *here* — it does not open a detail to ask on.

    The modal is the one the detail already raises: `ForceConfirmModal.for_record`, naming the
    session the way the row named it. What changes is where it is raised from, which is the
    whole of ask 6 for this key.
    """
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(120, 30)) as pilot:
        await _sessions_list(app, pilot)
        asking = await _press_force_on_the_list(app, pilot)
        step = position(app)
        modal = app.screen.is_modal
        status = _status(app)
        await pilot.press("escape")
        await _answered(asking)
        after = position(app)

    assert step == "FORCE_MODAL", f"`f` on the list reached {step} instead of the confirmation"
    assert modal, "the confirmation must be modal, or an app binding can leave it unanswered"
    assert launcher.issued == [], "force must not be issued before it is confirmed"
    (named,) = with_project_names((record,), (_PROJECT,))
    assert named.display.rendered in status, "the confirmation must name the session"
    assert after == "SESSIONS", f"aborting the modal left the owner on {after}, not the list"


async def test_aborting_force_from_the_list_issues_nothing_and_keeps_the_list() -> None:
    """An abort is a decision, and the owner is returned to what they were looking at.

    Escape rather than the abort row, because escape is the path that also has to unwind the
    modal without unwinding the position beneath it.
    """
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(120, 30)) as pilot:
        await _sessions_list(app, pilot)
        asking = await _press_force_on_the_list(app, pilot)
        await pilot.press("escape")
        await _answered(asking)
        step = position(app)
        rows = [str(o.prompt) for o in app.screen.query_one("#choices", OptionList).options]

    assert launcher.issued == [], f"an aborted force issued {launcher.issued}"
    assert step == "SESSIONS", f"the abort landed on {step}"
    assert len(rows) >= 1, "the list came back empty after an abort"


async def test_confirming_force_from_the_list_issues_one_force_and_keeps_the_list() -> None:
    """The whole of ask 6 for this key: it acts, and the owner is still on the list.

    Both halves asserted. A surface that issued the force and then pushed a detail — which is
    what this key used to do — would satisfy the count and still be the reported defect.
    """
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(120, 30)) as pilot:
        await _sessions_list(app, pilot)
        asking = await _press_force_on_the_list(app, pilot)
        assert position(app) == "FORCE_MODAL", "the modal did not open"
        # Moving off the abort row is the second deliberate act, exactly as it is from the
        # detail — the modal is the same one, so the way it is answered is the same too.
        await pilot.press("down")
        await pilot.press("enter")
        await _answered(asking)
        await pilot.pause()
        step = position(app)

    assert len(launcher.issued) == 1, f"a confirmed force issued {launcher.issued}"
    assert isinstance(launcher.issued[0], ForceStopCommand)
    assert step == "SESSIONS", f"a confirmed force navigated to {step} instead of staying"


@pytest.mark.parametrize("extra", [1, 2, 4, 9])
async def test_holding_the_force_key_down_on_the_list_destroys_nothing(extra: int) -> None:
    """`f` held down must not walk itself through its own confirmation.

    **The first press is driven as a task and the rest as ordinary presses, and the asymmetry
    is the mechanism rather than a convenience.** `f` on a row opens a modal and *suspends the
    handler* until it is answered — which is correct, and is what the detail's own force has
    always done — so `await pilot.press("f")` never returns and a loop of them deadlocks the
    test rather than failing it. That is exactly what the first version of this test did.

    What a held-down key actually produces is one press that opens the modal and a burst that
    arrives afterwards, by which time the modal is on top of the stack and is what they are
    delivered to. It binds no `f`, so they are inert — and that is the property worth pinning:
    the repeat lands somewhere that cannot act on it, rather than being refused by a guard
    somebody could later remove.

    **The first version of this docstring drew a wrong lesson from the same observation.** It
    said a suspending handler simply could not be driven by an awaited press, as though that
    were a fact about the harness. It was a fact about a **deadlock**: the key ran on the app's
    pump and froze the surface. `test_the_force_key_on_the_list_leaves_the_surface_answering`
    is the test that says so, and `RowStopAction` is the fix. This test keeps the
    `create_task` shape because what it pins is the *burst*, not the pump — but it is no longer
    the reason `f` cannot be pressed normally, because now it can be.
    """
    launcher = _RecordingLauncher(tuple(_record() for _ in range(3)))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(120, 30)) as pilot:
        await _sessions_list(app, pilot)
        asking = await _press_force_on_the_list(app, pilot)
        assert position(app) == "FORCE_MODAL", "the first press did not reach the confirmation"
        for _ in range(extra):
            await pilot.press("f")
            await pilot.pause()
        assert position(app) == "FORCE_MODAL", (
            "a repeated `f` moved the surface off the confirmation it had just opened"
        )
        await pilot.press("escape")
        await _answered(asking)
        step = position(app)

    assert launcher.issued == [], f"{extra + 1} `f` presses issued {launcher.issued}"
    assert step == "SESSIONS", f"the burst left the owner on {step}"


async def test_a_confirmed_force_that_raises_from_the_list_keeps_every_other_session() -> None:
    """The third path into the failure redraw, proven rather than argued.

    `s` and `c` reach `SessionsScreen.redraw_after_failure` directly; `f` reaches it through
    `confirm_force` → `tui.stop(FORCE, ...)`, which is the same override on the same screen.
    The Stage 2 Tier-1 re-review closed the Critical for `s`/`c` on the tests and for `f` on
    *inspection* — identical code path, identical override — and asked for this before calling
    the `f` path proven. It is the right ask: "the same code path" is a claim about the code as
    it is today, and the confirmation standing in front of this one is exactly the kind of
    thing that grows a branch of its own later.

    Three sessions, so "the others survived" is a claim the assertion can make; with one, a
    surviving row and a collapsed list are the same length.
    """

    def _raises() -> TerminalObservation:
        raise RuntimeError("tmux server is gone")

    # Through `force_result` rather than a new `error` field: this double already models "what
    # `force_stop` observed" as a callable, so a raising one is the shape it was built for and
    # needs no second mechanism that could drift from it.
    records = tuple(_record() for _ in range(3))
    launcher = _RecordingLauncher(records, force_result=_raises)
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(120, 30)) as pilot:
        await _sessions_list(app, pilot)
        rows_before = app.screen.query_one("#choices", OptionList).option_count
        assert rows_before == 3, f"the walk drew {rows_before} rows, not 3"

        asking = await _press_force_on_the_list(app, pilot)
        assert position(app) == "FORCE_MODAL", "the modal did not open"
        await pilot.press("down")
        await pilot.press("enter")
        await _answered(asking)
        await pilot.pause()

        step = position(app)
        choices = app.screen.query_one("#choices", OptionList)
        rows_after = choices.option_count
        cursor = choices.highlighted

    assert step == "SESSIONS", f"a failed force navigated to {step}"
    assert rows_after == 3, (
        f"a force that raised left {rows_after} rows instead of 3 — the other sessions were "
        f"hidden because one stop failed"
    )
    assert cursor is None, f"the cursor rests on row {cursor} after a failed force"


async def _pop_any_modal(app: RemoteAgentsTui) -> None:
    """Unblock a suspended confirmation so `run_test` teardown can finish.

    Only ever reached when an assertion below has already failed. Without it a deadlocked
    surface takes the whole *file* down with it — no failure, no timeout, no output — which is
    exactly the shape this test exists to catch, and a test that reproduces its own subject
    during teardown reports nothing at all.
    """
    while isinstance(app.screen, ConfirmScreen):
        app.pop_screen()
        await asyncio.sleep(0)


async def test_the_force_key_on_the_list_leaves_the_surface_answering() -> None:
    """`f` must open its confirmation **and leave the app alive**. It did not.

    **The seam this covers is the one every other list test steps over.** They all drive
    `asyncio.create_task(app.screen.action_row_action("force"))` — a task of the test's own
    making, which runs on whatever task the test is on and therefore cannot observe the pump a
    real keypress uses. Pressed for real, Textual dispatches a *screen's* binding from
    `App._on_key` → `run_action`, so the action runs on the **App's** message-pump task.
    `ask_to_confirm` suspends there and the app stops draining messages entirely: measured on
    the owner's real workstation, the modal came up correctly and then `Escape` did nothing,
    `ctrl+q` did nothing, and the process had to be killed. `SessionsPaneScreen` inherits the
    binding, so the console pane froze the same way.

    The detail's force is safe for the reason this now copies: it is reached from
    `on_option_list_option_selected`, a *message handler on the screen*, which runs on the
    screen's own pump — so a suspension there holds back only that screen's events, which is
    precisely the protection DEC-025 describes and the property its architecture sweep was
    written to preserve. That sweep checks the caller is a method on a `*Screen` class; it
    cannot see which pump the call arrives on, so `SessionsScreen.confirm_force` passed it
    while violating the decision in substance.

    Asserted as *the press returns and the surface still answers*, because that is the whole
    of the defect. A test that only checked the modal opened would have passed against a
    frozen application — the modal is the last thing it draws.
    """
    records = tuple(_record() for _ in range(2))
    launcher = _RecordingLauncher(records)
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(120, 30)) as pilot:
        await _sessions_list(app, pilot)
        try:
            await asyncio.wait_for(pilot.press("f"), timeout=5)
        except TimeoutError:
            await _pop_any_modal(app)
            raise AssertionError(
                "pressing `f` never returned: the confirmation suspended the app's message "
                "pump, so the whole surface stopped answering"
            ) from None

        assert position(app) == "FORCE_MODAL", f"`f` reached {position(app)}"

        try:
            await asyncio.wait_for(pilot.press("escape"), timeout=5)
        except TimeoutError:
            await _pop_any_modal(app)
            raise AssertionError(
                "the confirmation opened but escape never returned: the surface is frozen "
                "with a modal the owner cannot dismiss"
            ) from None

        step = position(app)

    assert step == "SESSIONS", f"aborting the confirmation left the owner on {step}"
    assert launcher.issued == [], "an abandoned confirmation issued a force stop"


async def test_aborting_the_force_modal_leaves_the_cursor_where_the_owner_put_it() -> None:
    """Aborting is a decision, and it must not rearm the key on a different session.

    `confirm_force`'s abort branch re-reads through `on_reveal`, which reset the cursor to row
    0. Measured before the fix: three RUNNING sessions, cursor on row 2, `f`, escape, then `s`
    — one graceful stop issued against **row 0**, a session the owner never pointed at.
    `check_action` does not save it, because row 0 is RUNNING and so genuinely offers
    `graceful`.

    The follow-up `s` is pressed rather than reasoned about, because the claim is about what
    the *next keypress* does and a cursor assertion alone would leave that inferred.
    """
    records = tuple(_record() for _ in range(3))
    launcher = _RecordingLauncher(records)
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test(size=(120, 30)) as pilot:
        await _sessions_list(app, pilot, index=2)
        choices = app.screen.query_one("#choices", OptionList)
        chosen = [option.id for option in choices.options][2]

        asking = await _press_force_on_the_list(app, pilot)
        assert position(app) == "FORCE_MODAL", "the modal did not open"
        await pilot.press("escape")
        await _answered(asking)
        await pilot.pause()

        after = app.screen.query_one("#choices", OptionList)
        keys = [option.id for option in after.options]
        cursor = after.highlighted
        landed = keys[cursor] if cursor is not None and keys else None

        # And what the next unconfirmed key actually does with it.
        await app.screen.action_row_action("graceful")
        await pilot.pause()
        await pilot.pause()

    assert landed in (chosen, None), (
        f"aborting moved the cursor from {chosen[:8]} to {landed and landed[:8]}"
    )
    stopped = [str(command.session_id) for command in launcher.issued]
    assert stopped in ([], [chosen]), (
        f"after aborting a force on {chosen[:8]}, `s` stopped {stopped} — the abort left the "
        f"cursor on a session the owner never selected"
    )
