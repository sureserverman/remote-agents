"""A session one surface starts must be fully manageable from the other."""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.codec import ManagedPane
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.adapters.tmux.gateway import TmuxInventory
from remote_agents.adapters.tmux.profiles import build_launch_profile
from remote_agents.adapters.tmux.runtime import TmuxTerminal
from remote_agents.application.commands import (
    CleanupCommand,
    GracefulStopCommand,
    InspectQuery,
    LaunchCommand,
)
from remote_agents.application.errors import DuplicateCommandError
from remote_agents.application.services import SessionService
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionState
from remote_agents.domain.profiles import closed_profiles

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


class RecordingGateway:
    """Model tmux ownership across two terminals without running a tmux server."""

    def __init__(self, intent_directory: Path) -> None:
        self.intent_directory = intent_directory
        self.panes: dict[SessionId, tuple[ProjectId, ProfileId]] = {}
        self.preserved: set[SessionId] = set()
        self.keys: list[tuple[SessionId, tuple[str, ...]]] = []

    async def launch(
        self, session_id: SessionId, project_id: ProjectId, profile_id: ProfileId, cwd: Path
    ) -> None:
        del cwd
        self.panes[session_id] = (project_id, profile_id)

    async def inventory(self) -> TmuxInventory:
        return TmuxInventory(
            tuple(
                ManagedPane(
                    f"ra-{session_id}",
                    session_id,
                    project_id,
                    profile_id,
                    1234,
                    session_id not in self.preserved,
                    session_id in self.preserved,
                )
                for session_id, (project_id, profile_id) in self.panes.items()
            ),
            (),
        )

    async def capture(self, session_id: SessionId) -> str:
        return "Claude Code ready"

    async def send_keys(self, session_id: SessionId, keys: tuple[str, ...]) -> None:
        self.keys.append((session_id, keys))
        self.preserved.add(session_id)


def _terminal(gateway: RecordingGateway, executable: Path) -> TmuxTerminal:
    """Compose a terminal exactly as the composition root does, with no static profiles."""
    definition = next(item for item in closed_profiles() if item.profile_id == ProfileId("claude"))
    return TmuxTerminal(
        gateway,
        {_PROJECT: executable.parent},
        {},
        startup_timeout=1,
        profile_factories={
            ProfileId("claude"): lambda session_id: build_launch_profile(
                definition, executable, session_id, {"PATH": "/usr/bin"}
            )
        },
    )


async def test_a_second_terminal_can_gracefully_stop_what_the_first_launched(
    database: Path, tmp_path: Path
) -> None:
    """The remembered profile is process-local, so the other surface must resolve its own."""
    executable = tmp_path / "bin" / "claude"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    gateway = RecordingGateway(tmp_path / "intents")
    terminal_connection = open_database(database)
    service_connection = open_database(database)
    try:
        terminal = SessionService(
            SQLiteSessionStore(terminal_connection), _terminal(gateway, executable)
        )
        service = SessionService(
            SQLiteSessionStore(service_connection), _terminal(gateway, executable)
        )

        launched = await terminal.launch(LaunchCommand(_PROJECT, _PROFILE, "tui-1"))
        stopped = await service.graceful_stop(GracefulStopCommand(launched.session_id, _PROFILE))

        assert stopped.preserved, "the other surface resolved no profile and sent no keys"
        assert gateway.keys and gateway.keys[0][0] == launched.session_id
        final = await service.list_sessions()
        assert [record.state for record in final] == [SessionState.PRESERVED]
    finally:
        terminal_connection.close()
        service_connection.close()
