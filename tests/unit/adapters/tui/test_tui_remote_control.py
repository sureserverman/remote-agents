"""Remote Control is offered only where the policy says it can work."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from textual.widgets import OptionList
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
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
        offered = any(key == "remote-control" for key in _keys(app))

    assert offered is remote_control_available(record)


async def test_the_toggle_requires_a_confirm_step() -> None:
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("remote-control")
        await pilot.pause()
        step = position(app)

    assert step == "REMOTE_CONTROL_CONFIRM"
    assert launcher.issued == [], "the first selection must not toggle anything"


async def test_confirming_issues_the_command_with_a_tui_idempotency_key() -> None:
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("remote-control")
        await pilot.pause()
        await app.resolve_remote_control("remote-control-active")
        await pilot.pause()

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
        await app.screen.choose("remote-control")
        await pilot.pause()
        await app.resolve_remote_control("remote-control-active")
        await pilot.pause()
        status = _status(app)

    assert "active" in status.casefold()


async def test_aborting_the_confirm_issues_nothing() -> None:
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("remote-control")
        await pilot.pause()
        await app.action_back()
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
        await app.screen.choose("remote-control")
        await pilot.pause()
        await app.resolve_remote_control("remote-control-active")
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
        # Even a stale key must not drive it.
        await app.screen.choose("remote-control")
        await pilot.pause()

    assert "remote-control" not in keys
    assert launcher.issued == []


async def test_a_failed_toggle_does_not_leave_the_cursor_on_the_button_that_failed() -> None:
    """Same class as the failed force stop: a repeat enter must not blindly re-issue."""
    record = _record()
    launcher = _RecordingLauncher((record,), error=RuntimeError("pane refused the toggle"))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("remote-control")
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert len(launcher.issued) == 1

        keys = _keys(app)
        resting = keys[app.screen.query_one("#choices").highlighted] if keys else None
        await pilot.press("enter")
        await pilot.pause()

    assert resting not in {"remote-control-active", "remote-control-inactive"}
    assert len(launcher.issued) == 1, "a repeated enter re-issued the toggle"
