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
from textual.widgets import OptionList
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.adapters.tui.model import _BACK
from remote_agents.application.commands import RemoteControlCommand
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
class _RecordingLauncher:
    records: tuple[SessionRecord, ...] = ()
    issued: list[RemoteControlCommand] = field(default_factory=list)
    result: RemoteControlState = RemoteControlState.ACTIVE
    error: Exception | None = None

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        return None

    async def set_remote_control(self, command: RemoteControlCommand) -> RemoteControlState:
        self.issued.append(command)
        if self.error is not None:
            raise self.error
        return self.result


def _context(launcher: _RecordingLauncher) -> TuiContext:
    return TuiContext(
        launcher=launcher,  # type: ignore[arg-type]
        creator=object(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_PROJECT,),
    )


def _keys(app: RemoteAgentsTui) -> list[str]:
    return [option.id for option in app.screen.query_one("#choices", OptionList).options]


def _status(app: RemoteAgentsTui) -> str:
    return str(app.screen.query_one("#status").content)


#: The two rows the detail offers when the policy allows the toggle at all.
_ENABLE = "remote-control-active"
_DISABLE = "remote-control-inactive"


async def _open_the_confirm(app: RemoteAgentsTui, pilot, key: str = _ENABLE) -> asyncio.Task:
    """Choose a direction on the detail and leave its confirmation open."""
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

    # Both directions or neither: the policy decides whether the toggle exists at all, and
    # nothing in the surface may offer one half of it, or a third thing beside it.
    assert offered == ({_ENABLE, _DISABLE} if remote_control_available(record) else set())


@pytest.mark.parametrize("key", [_ENABLE, _DISABLE])
async def test_each_direction_requires_a_confirm_step(key: str) -> None:
    """Neither direction changes anything on the first selection — parametrized because the
    detail now has two rows that mutate, and covering one would leave the other unpinned."""
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot, key)
        step = position(app)
        modal = app.screen.is_modal
        await pilot.press("escape")
        await asyncio.wait_for(asking, timeout=5)

    assert step == "REMOTE_CONTROL_MODAL"
    assert modal, "an app binding must not be able to leave this question unanswered"
    assert launcher.issued == [], "the first selection must not toggle anything"


@pytest.mark.parametrize(
    "key,desired",
    [(_ENABLE, RemoteControlState.ACTIVE), (_DISABLE, RemoteControlState.INACTIVE)],
)
async def test_the_confirmed_direction_is_the_one_the_row_asked_for(key, desired) -> None:
    """The direction is chosen on the detail, so it must survive the modal unchanged.

    This is what the three-row confirmation could not be asked: there, both directions were
    live until the last keypress, and a mis-wired row would have been indistinguishable from
    the owner choosing the other one.
    """
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        asking = await _open_the_confirm(app, pilot, key)
        await _confirm(pilot)
        await asyncio.wait_for(asking, timeout=5)

    assert [command.desired_state for command in launcher.issued] == [desired]


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
        status = _status(app)

    assert "pane refused the toggle" in status


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
        # Even a stale key must not drive it. Both directions, since either would be a way in.
        await asyncio.wait_for(app.screen.choose(_ENABLE), timeout=5)
        await asyncio.wait_for(app.screen.choose(_DISABLE), timeout=5)
        await pilot.pause()
        step = position(app)

    assert _ENABLE not in keys and _DISABLE not in keys
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
