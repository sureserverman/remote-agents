"""Force stop is irreversible, so it takes two deliberate choices and defaults to abort."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from remote_agents.adapters.tui.app import RemoteAgentsTui, Step
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.commands import ForceStopCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

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

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        return None

    async def force_stop(self, command: ForceStopCommand):
        self.issued.append(command)
        return None

    async def graceful_stop(self, command):
        self.issued.append(command)
        return None

    async def cleanup(self, command) -> None:
        self.issued.append(command)


def _context(launcher: _RecordingLauncher) -> TuiContext:
    return TuiContext(
        launcher=launcher,  # type: ignore[arg-type]
        creator=object(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_PROJECT,),
    )


def _rows(app: RemoteAgentsTui) -> list[str]:
    return [str(item.query_one("Label").content) for item in app.query("ListView > ListItem")]


def _keys(app: RemoteAgentsTui) -> list[str]:
    return [getattr(item, "entry_key", None) for item in app.query("ListView > ListItem")]


def _status(app: RemoteAgentsTui) -> str:
    return str(app.query_one("#status").content)


async def test_choosing_force_opens_a_confirm_step_and_issues_nothing_yet() -> None:
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("force")
        await pilot.pause()
        step = app._step
        status = _status(app)

    assert step is Step.FORCE_CONFIRM
    assert launcher.issued == [], "force must not be issued on the first selection"
    assert record.display.rendered in status, "the confirm step must name the session"


async def test_the_confirm_step_opens_with_abort_highlighted() -> None:
    """A stray enter must abort, not destroy — the wizard's review-before-mutate rule."""
    record = _record()
    app = RemoteAgentsTui(_context(_RecordingLauncher((record,))))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("force")
        await pilot.pause()
        highlighted = app.query_one("#choices").index
        keys = _keys(app)

    assert keys[highlighted] != "force-confirm", "the destructive option must not be preselected"


async def test_a_single_stray_enter_at_the_confirm_step_destroys_nothing() -> None:
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("force")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    assert launcher.issued == []


async def test_escape_at_the_confirm_step_aborts_and_issues_nothing() -> None:
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("force")
        await pilot.pause()
        await app.action_back()
        await pilot.pause()
        step = app._step

    assert launcher.issued == []
    assert step is Step.SESSION_DETAIL


async def test_only_the_second_confirmation_issues_the_force_stop() -> None:
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("force")
        await pilot.pause()
        assert launcher.issued == []
        await app._resolve_force_confirm("force-confirm")
        await pilot.pause()

    assert len(launcher.issued) == 1
    assert isinstance(launcher.issued[0], ForceStopCommand)
    assert launcher.issued[0].session_id == record.session_id


async def test_the_confirm_step_says_the_action_is_irreversible() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_RecordingLauncher((record,))))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("force")
        await pilot.pause()
        status = _status(app).casefold()

    assert "cannot be undone" in status or "irreversible" in status


async def test_aborting_returns_to_a_detail_that_still_offers_force() -> None:
    """Abort is not a dead end; the owner may have simply wanted to re-read the state."""
    record = _record()
    app = RemoteAgentsTui(_context(_RecordingLauncher((record,))))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("force")
        await pilot.pause()
        await app._resolve_force_confirm("\x00cancel")
        await pilot.pause()
        keys = _keys(app)

    assert "force" in keys


async def test_a_session_that_vanished_before_confirming_is_not_forced() -> None:
    record = _record()
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("force")
        await pilot.pause()
        launcher.records = ()
        await app._resolve_force_confirm("force-confirm")
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
