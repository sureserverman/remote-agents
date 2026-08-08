"""The terminal offers exactly the stops the policy allows, and no more."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.commands import (
    CleanupCommand,
    ForceStopCommand,
    GracefulStopCommand,
)
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_actions import available_actions
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_LABELS = {"Stop and close": "graceful", "Clean up": "cleanup", "Force stop": "force"}


def _record(state: SessionState) -> SessionRecord:
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
    """Records every command issued, so a stop that should not happen is visible."""

    records: tuple[SessionRecord, ...] = ()
    issued: list[object] = field(default_factory=list)
    error: Exception | None = None

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        return None

    async def graceful_stop(self, command: GracefulStopCommand):
        self.issued.append(command)
        if self.error is not None:
            raise self.error
        return None

    async def cleanup(self, command: CleanupCommand) -> None:
        self.issued.append(command)
        if self.error is not None:
            raise self.error

    async def force_stop(self, command: ForceStopCommand):
        self.issued.append(command)
        if self.error is not None:
            raise self.error
        return None


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


def _status(app: RemoteAgentsTui) -> str:
    return str(app.query_one("#status").content)


def _offered(app: RemoteAgentsTui) -> set[str]:
    return {_LABELS[row] for row in _rows(app) if row in _LABELS}


@pytest.mark.parametrize("state", list(SessionState))
async def test_detail_offers_exactly_the_policy_actions(state: SessionState) -> None:
    """No adapter-side addition or subtraction — the Stage 1 contract, now for the TUI."""
    record = _record(state)
    app = RemoteAgentsTui(_context(_RecordingLauncher((record,))))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        offered = _offered(app)

    assert offered == set(available_actions(state))


async def test_graceful_issues_a_graceful_stop_command() -> None:
    record = _record(SessionState.RUNNING)
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("graceful")
        await pilot.pause()

    assert len(launcher.issued) == 1
    issued = launcher.issued[0]
    assert isinstance(issued, GracefulStopCommand)
    assert issued.session_id == record.session_id
    assert issued.profile_id == record.profile_id


async def test_cleanup_issues_a_cleanup_command() -> None:
    record = _record(SessionState.PRESERVED)
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("cleanup")
        await pilot.pause()

    assert len(launcher.issued) == 1
    assert isinstance(launcher.issued[0], CleanupCommand)
    assert launcher.issued[0].session_id == record.session_id


async def test_a_failed_stop_reports_the_reason_and_does_not_claim_success() -> None:
    record = _record(SessionState.RUNNING)
    launcher = _RecordingLauncher((record,), error=RuntimeError("tmux server is gone"))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("graceful")
        await pilot.pause()
        status = _status(app)

    assert "tmux server is gone" in status
    assert "stopped" not in status.casefold()


async def test_a_failed_stop_re_renders_the_refreshed_state() -> None:
    """The owner must see what the session actually is now, not what it was."""
    record = _record(SessionState.RUNNING)
    launcher = _RecordingLauncher((record,), error=RuntimeError("nope"))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await app._resolve_detail("graceful")
        await pilot.pause()
        status = _status(app)

    assert "nope" in status


@pytest.mark.parametrize("state", list(SessionState))
async def test_an_action_the_policy_refuses_is_never_issued(state: SessionState) -> None:
    """Even if a stale entry key arrives, the surface must not act on it."""
    record = _record(state)
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        for action in ("graceful", "cleanup"):
            if action not in available_actions(state):
                await app._resolve_detail(action)
                await pilot.pause()

    assert launcher.issued == []


async def test_a_session_that_vanished_before_the_stop_is_not_stopped() -> None:
    record = _record(SessionState.RUNNING)
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        launcher.records = ()
        await app._resolve_detail("graceful")
        await pilot.pause()
        status = _status(app)

    assert launcher.issued == []
    assert "no longer available" in status.casefold()


async def test_the_busy_guard_is_held_until_the_post_stop_refresh_completes() -> None:
    """`_busy` must mean "no other action can run until this one's result is on screen".

    Releasing it before the refresh leaves a window where the step has already flipped but
    the list still holds the previous screen's entries, so a keypress in that window acts
    on a screen the owner is no longer looking at.
    """
    record = _record(SessionState.RUNNING)
    launcher = _RecordingLauncher((record,))
    observed: list[bool] = []

    class _Watching(RemoteAgentsTui):
        async def _show_detail(self, session_value: str) -> None:
            observed.append(self._busy)
            await super()._show_detail(session_value)

    app = _Watching(_context(launcher))

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        observed.clear()
        await app._resolve_detail("graceful")
        await pilot.pause()

    assert observed, "the post-stop refresh must happen"
    assert all(observed), "_busy was released before the refreshed screen was drawn"


async def test_a_navigation_action_cannot_interleave_with_a_stop() -> None:
    """Drives the race directly: escape fired while a slow stop is still in flight."""
    import asyncio

    record = _record(SessionState.RUNNING)

    @dataclass(slots=True)
    class _SlowLauncher:
        records: tuple[SessionRecord, ...] = ()
        issued: list[object] = field(default_factory=list)

        async def refresh_readiness(self):
            return self.records

        async def list_sessions(self):
            await asyncio.sleep(0)
            return self.records

        async def copy_attach(self, _session_id):
            return None

        async def graceful_stop(self, command):
            self.issued.append(command)
            await asyncio.sleep(0.02)
            return None

        async def cleanup(self, command) -> None:
            self.issued.append(command)

        async def force_stop(self, command):
            self.issued.append(command)
            return None

    launcher = _SlowLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await app._show_detail(str(record.session_id))
        await pilot.pause()
        await asyncio.gather(
            app._resolve_detail("graceful"),
            _press_escape_during(pilot),
        )
        await pilot.pause()

    assert len(launcher.issued) == 1, "the stop must be issued exactly once"


async def _press_escape_during(pilot) -> None:
    import asyncio

    await asyncio.sleep(0.005)
    await pilot.press("escape")
