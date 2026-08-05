"""A session one surface starts must be fully manageable from the other."""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.application.commands import (
    CleanupCommand,
    GracefulStopCommand,
    InspectQuery,
    LaunchCommand,
)
from remote_agents.application.errors import DuplicateCommandError
from remote_agents.application.services import SessionService
from remote_agents.domain.models import ProfileId, ProjectId, SessionState

_PROJECT = ProjectId("opaque-project")
_PROFILE = ProfileId("claude")


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "sessions.sqlite3"


async def test_the_service_manages_a_session_the_terminal_started(database: Path) -> None:
    """The terminal is a second process; only the shared store connects the two."""
    terminal_connection = open_database(database)
    service_connection = open_database(database)
    try:
        shared_panes = FakeTerminal()
        terminal = SessionService(SQLiteSessionStore(terminal_connection), shared_panes)
        service = SessionService(SQLiteSessionStore(service_connection), shared_panes)

        launched = await terminal.launch(LaunchCommand(_PROJECT, _PROFILE, "tui-1", "from tui"))

        listed = await service.list_sessions()
        assert [record.session_id for record in listed] == [launched.session_id]
        assert listed[0].display.custom_label == "from tui"

        observation = await service.inspect(InspectQuery(launched.session_id))
        assert observation is not None and observation.live

        stopped = await service.graceful_stop(GracefulStopCommand(launched.session_id, _PROFILE))
        assert stopped.preserved
        await service.cleanup(CleanupCommand(launched.session_id))

        final = await terminal.list_sessions()
        assert [record.state for record in final] == [SessionState.ENDED]
    finally:
        terminal_connection.close()
        service_connection.close()


async def test_an_idempotency_key_cannot_be_replayed_from_the_other_process(
    database: Path,
) -> None:
    """The in-process locks are not shared, so the durable claim is what prevents a double."""
    terminal_connection = open_database(database)
    service_connection = open_database(database)
    try:
        panes = FakeTerminal()
        terminal = SessionService(SQLiteSessionStore(terminal_connection), panes)
        service = SessionService(SQLiteSessionStore(service_connection), panes)

        await terminal.launch(LaunchCommand(_PROJECT, _PROFILE, "shared-key"))

        with pytest.raises(DuplicateCommandError):
            await service.launch(LaunchCommand(_PROJECT, _PROFILE, "shared-key"))

        assert len(await service.list_sessions()) == 1
    finally:
        terminal_connection.close()
        service_connection.close()


async def test_each_surface_allocates_its_own_sequence_from_the_shared_store(
    database: Path,
) -> None:
    terminal_connection = open_database(database)
    service_connection = open_database(database)
    try:
        panes = FakeTerminal()
        terminal = SessionService(SQLiteSessionStore(terminal_connection), panes)
        service = SessionService(SQLiteSessionStore(service_connection), panes)

        first = await terminal.launch(LaunchCommand(_PROJECT, _PROFILE, "tui-1"))
        second = await service.launch(LaunchCommand(_PROJECT, _PROFILE, "bot-1"))

        assert first.display.sequence == 1
        assert second.display.sequence == 2
    finally:
        terminal_connection.close()
        service_connection.close()


async def test_a_terminal_session_is_visible_to_a_service_started_afterwards(
    database: Path,
) -> None:
    """The service usually starts after the terminal; it must still see what is running."""
    terminal_connection = open_database(database)
    try:
        panes = FakeTerminal()
        terminal = SessionService(SQLiteSessionStore(terminal_connection), panes)
        launched = await terminal.launch(LaunchCommand(_PROJECT, _PROFILE, "tui-1"))
    finally:
        terminal_connection.close()

    service_connection = open_database(database)
    try:
        service = SessionService(SQLiteSessionStore(service_connection), FakeTerminal())
        listed = await service.list_sessions()

        assert [record.session_id for record in listed] == [launched.session_id]
    finally:
        service_connection.close()
