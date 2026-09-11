"""Remote Control is offered only where the policy says it can work.

Both directions are rows on the session detail now — "Enable Remote Control" and "Disable
Remote Control", matching the bot — and each opens a modal confirming that one direction. The
three-row confirmation screen this replaces was a chooser rather than a confirmation: the
direction was still undecided when the question was asked, which is why it could not be
answered with a yes or a no.

That reshapes how these tests drive it. Selecting a direction suspends its handler until the
modal is answered, so the choice runs as a task, the answer is real keys, and the test joins.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from backends import SessionUseCaseDouble, backend_for
from textual.widgets import OptionList, Static
from tui_feedback import announcements
from tui_feedback import status as _status
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.model import _BACK
from remote_agents.application.commands import RemoteControlCommand
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_actions import remote_control_available
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import RemoteControlState

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")


def _record(state: SessionState = SessionState.RUNNING, profile: str = "claude") -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId(profile),
        SessionDisplayIdentity("existing", profile, "regular", 1),
        state,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _RecordingLauncher(SessionUseCaseDouble):
    records: tuple[SessionRecord, ...] = ()
    issued: list[RemoteControlCommand] = field(default_factory=list)
    result: RemoteControlState = RemoteControlState.ACTIVE
    error: Exception | None = None
    #: What the pane reads as when the confirmation asks. UNKNOWN by default, which is what
    #: an unarmed double honestly has and what DEC-003 lets the surface still propose.
    reading: RemoteControlState = RemoteControlState.UNKNOWN
    reads: int = 0

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        return None

    async def remote_control_state(self, _session_id) -> RemoteControlState:
        self.reads += 1
        return self.reading

    async def set_remote_control(self, command: RemoteControlCommand) -> RemoteControlState:
        self.issued.append(command)
        if self.error is not None:
            raise self.error
        return self.result


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


def _keys(app: RemoteAgentsTui) -> list[str]:
    return [option.id for option in app.screen.query_one("#choices", OptionList).options]


#: The one row the detail offers when the policy allows the toggle at all. It was a pair
#: (`remote-control-active` / `remote-control-inactive`) until 2026-09-11: the row named the
#: direction a press would take, which this surface cannot promise before the pane has been
#: read. The read happens in the confirmation now, so the row names only its subject.
_TOGGLE = "remote-control"


async def _open_the_confirm(app: RemoteAgentsTui, pilot, key: str = _TOGGLE) -> asyncio.Task:
    """Choose the toggle on the detail and leave its confirmation open."""
    task = asyncio.create_task(app.screen.choose(key))
    await pilot.pause()
    return task


async def _confirm(pilot) -> None:
    """Move off the resting Cancel and answer yes."""
    await pilot.press("down")
    await pilot.press("enter")


@pytest.mark.parametrize("state", list(SessionState))
@pytest.mark.parametrize("profile", ["claude", "codex", "cursor"])
async def test_the_toggle_is_offered_exactly_where_the_policy_allows_it(
    state: SessionState, profile: str
) -> None:
    record = _record(state, profile)
    app = RemoteAgentsTui(_context(_RecordingLauncher((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        # Every remote-control row, not the two this test expects. Filtering to the expected
        # pair would have made the assertion blind to a *third* one — including the single
        # `remote-control` row this stage replaced, which would then have sat on the detail
        # opening nothing, and passed. Found by sweeping for that key at the stage gate.
        offered = {key for key in _keys(app) if key and key.startswith("remote-control")}

    # Exactly the one row, or none. Still swept by prefix rather than compared to the key
    # this test expects: a *second* remote-control row -- one of the retired direction pair
    # left behind, say -- would sit on the detail opening nothing, and a filtered assertion
    # would pass over it. That sweep caught one at this stage's own gate once already.
    assert offered == ({_TOGGLE} if remote_control_available(record) else set())


async def test_the_toggle_requires_a_confirm_step() -> None:
    """Selecting the row changes nothing: the press opens a question, it does not answer one."""
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot)
        step = position(app)
        modal = app.screen.is_modal
        await pilot.press("escape")
        await asyncio.wait_for(asking, timeout=5)

    assert step == "REMOTE_CONTROL_MODAL"
    assert modal, "an app binding must not be able to leave this question unanswered"
    assert launcher.issued == [], "the first selection must not toggle anything"


@pytest.mark.parametrize(
    "reading,desired,named",
    [
        (RemoteControlState.ACTIVE, RemoteControlState.INACTIVE, "Turn off"),
        (RemoteControlState.INACTIVE, RemoteControlState.ACTIVE, "Turn on"),
        (RemoteControlState.UNKNOWN, RemoteControlState.ACTIVE, "Turn on"),
    ],
)
async def test_the_confirmed_direction_is_the_one_the_pane_read_implies(
    reading, desired, named
) -> None:
    """One row, and the reading taken when it is chosen decides what confirming will do.

    The direction used to be chosen on the detail from the *stored* observation, which is as
    old as the last toggle -- so a pane the owner had since changed from inside Claude could
    be offered a row that confidently named the wrong way. The reading is taken here instead,
    between the press and the question, and the question says which way it found.

    UNKNOWN proposes *on*: enabling is one curated sequence, while disabling opens Claude's
    status menu and arrows through it, so a disable aimed at a pane that was not where we
    thought it was leaves a menu open in somebody's session (DEC-003).
    """
    record = _record()
    launcher = _RecordingLauncher((record,), reading=reading)
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot)
        # `#status` is the modal's prompt: `ConfirmScreen.compose` deliberately reuses the
        # shared body's ids so a renamed widget cannot hide from the checks that matter.
        prompt = app.screen.query_one("#status", Static)
        question = " ".join(
            prompt.render_line(row).text for row in range(max(prompt.size.height, 0))
        )
        await _confirm(pilot)
        await asyncio.wait_for(asking, timeout=5)

    assert launcher.reads == 1, "the confirmation reads the pane, once, per press"
    assert [command.desired_state for command in launcher.issued] == [desired]
    assert named.casefold() in question.casefold(), question


async def test_confirming_issues_the_command_with_a_tui_idempotency_key() -> None:
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot)
        await _confirm(pilot)
        await asyncio.wait_for(asking, timeout=5)

    assert len(launcher.issued) == 1
    command = launcher.issued[0]
    assert command.session_id == record.session_id
    assert command.desired_state is RemoteControlState.ACTIVE
    assert command.idempotency_key.startswith("tui-")


async def test_the_returned_state_is_surfaced() -> None:
    record = _record()
    launcher = _RecordingLauncher((record,), result=RemoteControlState.ACTIVE)
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot)
        await _confirm(pilot)
        await asyncio.wait_for(asking, timeout=5)
        await pilot.pause()
        status = _status(app)

    assert "active" in status.casefold()


async def test_aborting_the_confirm_issues_nothing() -> None:
    """Escape, not `action_back`: the app binding cannot reach a modal, which is the point.

    The earlier version drove `app.action_back()` directly, which was the same thing while
    the confirmation was an ordinary screen. Under the modal it is not — the app's escape
    binding is out of the chain — so calling it would prove that a path nobody can take
    changes nothing. The key is what the owner has.
    """
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot)
        await pilot.press("escape")
        await asyncio.wait_for(asking, timeout=5)
        await pilot.pause()
        step = position(app)

    assert launcher.issued == []
    assert step == "SESSION_DETAIL"


async def test_a_failure_reports_itself_and_does_not_claim_a_state() -> None:
    record = _record()
    launcher = _RecordingLauncher((record,), error=RuntimeError("pane refused the toggle"))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot)
        await _confirm(pilot)
        await asyncio.wait_for(asking, timeout=5)
        await pilot.pause()
        reported = announcements(app, severity="error")

    assert any("pane refused the toggle" in message for message in reported), reported


async def test_no_version_gating_is_applied_to_the_toggle() -> None:
    """DEC-002: agent versions are owner-managed and never gate an action."""
    record = _record()
    app = RemoteAgentsTui(_context(_RecordingLauncher((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        status = _status(app).casefold()

    assert "version" not in status


async def test_a_non_claude_session_offers_no_toggle_even_when_running() -> None:
    record = _record(SessionState.RUNNING, "codex")
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        keys = _keys(app)
        # Even a stale key must not drive it. The two retired direction keys are pressed
        # beside the live one: a screen that still answered either of them would be a way in
        # that nothing else here would notice, since neither is drawn any more.
        for stale in (_TOGGLE, "remote-control-active", "remote-control-inactive"):
            await asyncio.wait_for(app.screen.choose(stale), timeout=5)
        await pilot.pause()
        step = position(app)

    assert not [key for key in keys if key and key.startswith("remote-control")]
    assert launcher.issued == []
    assert step == "SESSION_DETAIL", "a stale key opened a confirmation the policy forbids"


async def test_a_failed_toggle_does_not_leave_the_cursor_on_the_button_that_failed() -> None:
    """Same class as the failed force stop: a repeat enter must not blindly re-issue."""
    record = _record()
    launcher = _RecordingLauncher((record,), error=RuntimeError("pane refused the toggle"))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot)
        await _confirm(pilot)
        await asyncio.wait_for(asking, timeout=5)
        await pilot.pause()
        assert len(launcher.issued) == 1

        keys = _keys(app)
        resting = keys[app.screen.query_one("#choices").highlighted] if keys else None
        await pilot.press("enter")
        await pilot.pause()

    # Asserted against the failure path's actual post-condition rather than against the
    # modal's row ids: those cannot appear here at all now, so excluding them would be an
    # assertion that cannot fail. The detail is left offering one Back row, resting on it.
    assert keys == [_BACK], f"a failed toggle left the detail offering {keys}"
    assert resting == _BACK, "the cursor must be moved off every row that acts"
    assert len(launcher.issued) == 1, "a repeated enter re-issued the toggle"
