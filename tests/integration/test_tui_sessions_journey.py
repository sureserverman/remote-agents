"""The terminal can see and reach a session another process launched.

This is the recovery hole Stage 2 closes. The session is launched through a *separate*
composition on its own SQLite connection — the shape a running `serve` actually has — so a
shared in-process object cannot make the test pass by accident.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.codec import attach_argv
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.adapters.tui.app import RemoteAgentsTui, Step
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.services import SessionService
from remote_agents.domain.models import ProfileId, ProjectId, SessionId

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")


def _rows(app: RemoteAgentsTui) -> list[str]:
    return [str(item.query_one("Label").content) for item in app.query("ListView > ListItem")]


def _status(app: RemoteAgentsTui) -> str:
    return str(app.query_one("#status").content)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "sessions.sqlite3"


async def test_the_terminal_lists_inspects_and_reaches_a_session_it_never_launched(
    database: Path,
) -> None:
    service_connection = open_database(database)
    terminal_connection = open_database(database)
    try:
        # Connection A — stands in for the running service.
        terminal = FakeTerminal()
        service = SessionService(SQLiteSessionStore(service_connection), terminal)
        launched = await service.launch(
            LaunchCommand(ProjectId("opaque-existing"), ProfileId("claude"), "service-key")
        )

        # Connection B — the terminal's own composition, sharing only the database file.
        context = TuiContext(
            launcher=SessionService(SQLiteSessionStore(terminal_connection), terminal),
            creator=object(),  # type: ignore[arg-type]
            profiles=(ProfileChoice("claude", True),),
            refresh_catalogue=lambda: (_PROJECT,),
            attach_argv=lambda session_id: attach_argv(SessionId.parse(session_id)),
            catalogue=(_PROJECT,),
        )
        app = RemoteAgentsTui(context)

        async with app.run_test() as pilot:
            await pilot.press("ctrl+s")
            await pilot.pause()

            rows = _rows(app)
            assert len(rows) == 1, "the terminal must see the service's session"
            assert "running" in rows[0]

            await pilot.press("enter")
            await pilot.pause()
            assert app._step is Step.SESSION_DETAIL
            detail = _status(app)
            assert str(launched.display.rendered) in detail
            assert "running" in detail

            await app._resolve_detail("attach")
            await pilot.pause()
            attach = _status(app)

        assert str(launched.session_id) in attach, "the attach command must name the session"
        # Byte for byte what the owner would paste, from the same codec production uses.
        assert " ".join(attach_argv(launched.session_id)) in attach
    finally:
        service_connection.close()
        terminal_connection.close()


async def test_a_session_stopped_by_the_service_leaves_the_terminal_list(
    database: Path,
) -> None:
    """The terminal reads the store live rather than caching what it first saw."""
    service_connection = open_database(database)
    terminal_connection = open_database(database)
    try:
        terminal = FakeTerminal()
        service = SessionService(SQLiteSessionStore(service_connection), terminal)
        launched = await service.launch(
            LaunchCommand(ProjectId("opaque-existing"), ProfileId("claude"), "service-key")
        )
        context = TuiContext(
            launcher=SessionService(SQLiteSessionStore(terminal_connection), terminal),
            creator=object(),  # type: ignore[arg-type]
            profiles=(ProfileChoice("claude", True),),
            refresh_catalogue=lambda: (_PROJECT,),
            attach_argv=lambda session_id: attach_argv(SessionId.parse(session_id)),
            catalogue=(_PROJECT,),
        )
        app = RemoteAgentsTui(context)

        async with app.run_test() as pilot:
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert len(_rows(app)) == 1

            from remote_agents.application.commands import ForceStopCommand

            await service.force_stop(ForceStopCommand(launched.session_id))

            await pilot.press("ctrl+s")
            await pilot.pause()
            rows = _rows(app)
            status = _status(app)

        assert rows == []
        assert "no managed sessions" in status.casefold()
    finally:
        service_connection.close()
        terminal_connection.close()
