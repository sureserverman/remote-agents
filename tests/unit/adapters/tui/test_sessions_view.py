"""The local surface can see every managed session, not only the one it launched."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from remote_agents.adapters.tui.app import RemoteAgentsTui, Step
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_EXISTING = CatalogProject("opaque-existing", "existing", "infra", "Registered")


def _record(state: SessionState = SessionState.RUNNING, *, minutes: int = 0) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        state,
        datetime.now(UTC) - timedelta(minutes=minutes),
    )


@dataclass(slots=True)
class _Listing:
    """A launcher that reports whatever set of sessions the test asked for."""

    records: tuple[SessionRecord, ...] = ()
    refreshed: int = 0
    list_error: Exception | None = None

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        self.refreshed += 1
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        if self.list_error is not None:
            raise self.list_error
        return self.records

    async def copy_attach(self, _session_id) -> str | None:
        return None

    async def launch(self, _command):  # pragma: no cover - wizard path, unused here
        raise AssertionError("the sessions view must not launch anything")


def _context(launcher: _Listing) -> TuiContext:
    return TuiContext(
        launcher=launcher,  # type: ignore[arg-type]
        creator=object(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_EXISTING,),
        attach_argv=lambda session_id: (
            "tmux",
            "-L",
            "remote-agents",
            "attach-session",
            "-t",
            f"={session_id}",
        ),
        catalogue=(_EXISTING,),
    )


def _rows(app: RemoteAgentsTui) -> list[str]:
    return [str(item.query_one("Label").content) for item in app.query("ListView > ListItem")]


def _status(app: RemoteAgentsTui) -> str:
    return str(app.query_one("#status").content)


async def test_sessions_lists_one_row_per_managed_session() -> None:
    launcher = _Listing((_record(SessionState.RUNNING), _record(SessionState.PRESERVED)))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        rows = _rows(app)

    assert len(rows) == 2
    assert all("existing" in row for row in rows)


async def test_sessions_refreshes_readiness_before_listing() -> None:
    """A session that became ready elsewhere must not still read as failed here."""
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()

    assert launcher.refreshed == 1


async def test_each_row_names_the_project_state_and_age() -> None:
    launcher = _Listing((_record(SessionState.RUNNING, minutes=7),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        row = _rows(app)[0]

    assert "existing" in row
    assert "running" in row
    assert "7m" in row


async def test_ended_sessions_are_filtered_exactly_as_the_bot_filters_them() -> None:
    launcher = _Listing((_record(SessionState.ENDED), _record(SessionState.RUNNING)))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        rows = _rows(app)

    assert len(rows) == 1
    assert "running" in rows[0]


async def test_no_managed_session_renders_an_explicit_empty_state() -> None:
    app = RemoteAgentsTui(_context(_Listing(())))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        status = _status(app)
        rows = _rows(app)

    assert "no managed sessions" in status.casefold()
    assert rows == []


async def test_every_ended_list_still_renders_the_empty_state() -> None:
    """Filtering must not leave the owner staring at a list that claims sessions exist."""
    app = RemoteAgentsTui(_context(_Listing((_record(SessionState.ENDED),))))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        status = _status(app)

    assert "no managed sessions" in status.casefold()


async def test_a_store_error_reports_itself_and_leaves_the_wizard_reachable() -> None:
    launcher = _Listing((), list_error=RuntimeError("database is locked"))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        await app.action_sessions()
        await pilot.pause()
        status = _status(app)
        await app.action_back()
        await pilot.pause()
        step = app._step

    assert "could not be read" in status.casefold()
    assert step is Step.PROJECTS


async def test_the_sessions_step_does_not_disturb_a_launch_in_progress() -> None:
    launcher = _Listing((_record(),))
    app = RemoteAgentsTui(_context(launcher))

    async with app.run_test() as pilot:
        app._busy = True
        await app.action_sessions()
        await pilot.pause()
        step = app._step

    assert step is Step.PROJECTS
    assert launcher.refreshed == 0
