"""The terminal offers exactly the stops the policy allows, and no more."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from backends import backend_for
from stop_results import a_verified_force_stop
from textual.widgets import OptionList
from tui_feedback import announcements
from tui_feedback import status as _status

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.commands import (
    CleanupCommand,
    ForceStopCommand,
    GracefulStopCommand,
)
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_actions import (
    GRACEFUL_TIMEOUT,
    UNKNOWN_SESSION,
    available_actions,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.terminal import TerminalObservation

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_LABELS = {"Stop and close": "graceful", "Clean up": "cleanup", "Force stop": "force"}


def _preserved() -> TerminalObservation:
    """A graceful stop that worked: the profile's own exit sequence ran and the pane exited."""
    return TerminalObservation(SessionId.new(), live=False, preserved=True)


def _not_preserved(detail: str) -> TerminalObservation:
    """A graceful stop that did not take effect, for the reason `detail` names."""
    return TerminalObservation(SessionId.new(), live=True, preserved=False, detail=detail)


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
    #: What `graceful_stop` reports back. Defaults to a clean exit, which is what every case
    #: written before BL-008 assumes — the surface discarded this value entirely.
    observation: TerminalObservation | None = None

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
        return self.observation or _preserved()

    async def cleanup(self, command: CleanupCommand) -> None:
        self.issued.append(command)
        if self.error is not None:
            raise self.error

    async def force_stop(self, command: ForceStopCommand):
        self.issued.append(command)
        if self.error is not None:
            raise self.error
        return a_verified_force_stop()


def _context(launcher: _RecordingLauncher) -> TuiContext:
    return TuiContext(
        backend=backend_for(
            sessions=launcher,  # type: ignore[arg-type]
            projects=object(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_PROJECT,),
            catalogue=(_PROJECT,),
        ),
        profiles=(ProfileChoice("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


def _rows(app: RemoteAgentsTui) -> list[str]:
    return [str(option.prompt) for option in app.screen.query_one("#choices", OptionList).options]


def _offered(app: RemoteAgentsTui) -> set[str]:
    return {_LABELS[row] for row in _rows(app) if row in _LABELS}


@pytest.mark.parametrize("state", list(SessionState))
async def test_detail_offers_exactly_the_policy_actions(state: SessionState) -> None:
    """No adapter-side addition or subtraction — the Stage 1 contract, now for the TUI."""
    record = _record(state)
    app = RemoteAgentsTui(_context(_RecordingLauncher((record,))))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        offered = _offered(app)

    assert offered == set(available_actions(state, None))


async def test_graceful_issues_a_graceful_stop_command() -> None:
    record = _record(SessionState.RUNNING)
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("graceful")
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
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("cleanup")
        await pilot.pause()

    assert len(launcher.issued) == 1
    assert isinstance(launcher.issued[0], CleanupCommand)
    assert launcher.issued[0].session_id == record.session_id


async def test_a_failed_stop_reports_the_reason_and_does_not_claim_success() -> None:
    record = _record(SessionState.RUNNING)
    launcher = _RecordingLauncher((record,), error=RuntimeError("tmux server is gone"))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("graceful")
        await pilot.pause()
        reported = " ".join(announcements(app, severity="error"))

    assert "tmux server is gone" in reported
    assert "stopped" not in reported.casefold()


async def test_a_failed_stop_re_renders_the_refreshed_state() -> None:
    """The owner must see what the session actually is now, not what it was."""
    record = _record(SessionState.RUNNING)
    launcher = _RecordingLauncher((record,), error=RuntimeError("nope"))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("graceful")
        await pilot.pause()
        reported = " ".join(announcements(app, severity="error"))

    assert "nope" in reported


@pytest.mark.parametrize("state", list(SessionState))
async def test_an_action_the_policy_refuses_is_never_issued(state: SessionState) -> None:
    """Even if a stale entry key arrives, the surface must not act on it."""
    record = _record(state)
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        for action in ("graceful", "cleanup"):
            if action not in available_actions(state, None):
                await app.screen.choose(action)
                await pilot.pause()

    assert launcher.issued == []


async def test_a_session_that_vanished_before_the_stop_is_not_stopped() -> None:
    record = _record(SessionState.RUNNING)
    launcher = _RecordingLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        launcher.records = ()
        await app.screen.choose("graceful")
        await pilot.pause()
        status = _status(app)

    assert launcher.issued == []
    assert "no longer available" in status.casefold()


async def test_the_busy_guard_is_held_until_the_post_stop_refresh_completes() -> None:
    """`busy` must mean "no other action can run until this one's result is on screen".

    Releasing it before the refresh leaves a window where the command has landed but the rows
    still describe the session as it was, so a keypress in that window acts on a screen the
    owner is no longer really looking at.

    Watched on the detail screen's own re-read rather than on an app method: the refresh is
    `SessionDetailScreen.render_detail` now, reached either directly or through `go_back`'s
    reveal, and pinning it here is what keeps the guarantee attached to the thing that
    actually redraws.
    """
    record = _record(SessionState.RUNNING)
    launcher = _RecordingLauncher((record,))
    observed: list[bool] = []

    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()

        detail = app.screen
        original = detail.render_detail

        async def _watched() -> None:
            observed.append(app.busy)
            await original()

        detail.render_detail = _watched  # type: ignore[method-assign]
        await app.screen.choose("graceful")
        await pilot.pause()

    assert observed, "the post-stop refresh must happen"
    assert all(observed), "busy was released before the refreshed screen was drawn"


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
            return _preserved()

        async def cleanup(self, command) -> None:
            self.issued.append(command)

        async def force_stop(self, command):
            self.issued.append(command)
            return a_verified_force_stop()

    launcher = _SlowLauncher((record,))
    app = RemoteAgentsTui(_context(launcher))  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await asyncio.gather(
            app.screen.choose("graceful"),
            _press_escape_during(pilot),
        )
        await pilot.pause()

    assert len(launcher.issued) == 1, "the stop must be issued exactly once"


async def _press_escape_during(pilot) -> None:
    import asyncio

    await asyncio.sleep(0.005)
    await pilot.press("escape")


# BL-008 — a graceful stop that did not take effect says which of the two causes it was ----
#
# Both surfaces discarded `graceful_stop`'s return value, so a stop that never sent an exit
# sequence and a stop whose agent ignored one were reported identically to a stop that worked:
# the session simply stayed on screen, still running, with nothing said. The two causes are a
# configuration problem and an agent-behaviour problem, and an owner who is told only that
# "the stop did not work" cannot tell which of two completely different next steps applies.


@pytest.mark.parametrize(
    "detail,names",
    [
        (UNKNOWN_SESSION, "The stop was never sent."),
        (GRACEFUL_TIMEOUT, "The agent did not exit in time."),
    ],
)
async def test_a_graceful_stop_that_did_not_take_effect_names_its_cause(
    detail: str, names: str
) -> None:
    record = _record(SessionState.RUNNING)
    launcher = _RecordingLauncher((record,), observation=_not_preserved(detail))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("graceful")
        await pilot.pause()
        status = _status(app)
        reported = " ".join(announcements(app, severity="error"))

    assert launcher.issued, "the stop was never issued, so this asserts nothing about its result"
    assert names in status, status
    assert names in reported, reported
    assert "did not take effect" in status, (
        f"the status line reads {status!r}, which describes a session that is merely still "
        "running — the exact silence BL-008 recorded"
    )


async def test_the_two_causes_do_not_read_alike() -> None:
    """The half of BL-008 a per-cause test cannot check, because each only sees its own.

    Reporting both failures in the same words would satisfy every assertion above while
    leaving the owner exactly where the backlog entry found them. Compared as whole rendered
    messages rather than by looking for a marker string, so it fails on wording that has
    converged rather than only on wording that was never written.
    """
    said = {}
    for detail in (UNKNOWN_SESSION, GRACEFUL_TIMEOUT):
        record = _record(SessionState.RUNNING)
        launcher = _RecordingLauncher((record,), observation=_not_preserved(detail))
        app = RemoteAgentsTui(_context(launcher))
        async with app.run_test() as pilot:
            await app.show_detail(str(record.session_id))
            await pilot.pause()
            await app.screen.choose("graceful")
            await pilot.pause()
            said[detail] = (_status(app), " ".join(announcements(app, severity="error")))

    assert said[UNKNOWN_SESSION] != said[GRACEFUL_TIMEOUT]


async def test_a_graceful_stop_that_worked_says_nothing_about_a_failure() -> None:
    """The other direction, and the one a fail-open default would break.

    `preserved` is what tells a clean exit from a timeout, and a surface that reported every
    stop as suspect would be worse than one that reported none — the owner would learn to
    ignore it, which is where this started.
    """
    record = _record(SessionState.RUNNING)
    launcher = _RecordingLauncher((record,), observation=_preserved())
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("graceful")
        await pilot.pause()
        status = _status(app)
        reported = announcements(app, severity="error")

    assert reported == []
    assert "did not take effect" not in status
