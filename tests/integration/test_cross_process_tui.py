"""A stop issued from the terminal must not depend on who launched the session (DEC-006).

The two compositions here are genuinely independent: separate SQLite connections *and*
separate `TmuxTerminal` instances, each with its own process-local profile cache. Only the
database file and the tmux gateway are shared, which is exactly what two real processes
share. A single shared terminal double cannot detect this defect class — it would carry the
launching process's remembered profile straight into the stopping process.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.codec import ManagedPane
from remote_agents.adapters.tmux.gateway import TmuxInventory
from remote_agents.adapters.tmux.profiles import build_launch_profile
from remote_agents.adapters.tmux.runtime import TmuxTerminal
from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.services import SessionService
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionState
from remote_agents.domain.profiles import closed_profiles


class RecordingGateway:
    """Model tmux ownership across two terminals without running a tmux server.

    Shared between the two compositions on purpose: a real tmux server is shared between
    real processes. What is *not* shared is the TmuxTerminal wrapping it, which is where
    the process-local profile cache lives and therefore where the defect would hide.
    """

    def __init__(self, intent_directory: Path) -> None:
        self.intent_directory = intent_directory
        self.panes: dict[SessionId, tuple[ProjectId, ProfileId]] = {}
        self.preserved: set[SessionId] = set()
        self.keys: list[tuple[SessionId, tuple[str, ...]]] = []
        self.mutations: list[tuple[str, str]] = []

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

    async def mutate(self, verb: str, target: str) -> None:
        """Model `kill-session`, which is how force stop and cleanup retire a pane."""
        self.mutations.append((verb, target))
        for session_id in list(self.panes):
            if target == f"ra-{session_id}":
                del self.panes[session_id]
                self.preserved.discard(session_id)


_PROJECT = ProjectId("opaque-project")
_PROFILE = ProfileId("claude")
_CATALOGUE = (CatalogProject("opaque-project", "existing", "infra", "Registered"),)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "sessions.sqlite3"


@pytest.fixture
def executable(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    return path


def _terminal(
    gateway: RecordingGateway, executable: Path, *, profiles: bool = True
) -> TmuxTerminal:
    """Compose a terminal as the composition root does, with no pre-resolved profiles.

    `profiles=False` composes one that knows no launch factories at all, standing in for a
    host whose configuration no longer curates the profile a stored session was started
    with.
    """
    definition = next(item for item in closed_profiles() if item.profile_id == _PROFILE)
    factories = (
        {
            _PROFILE: lambda session_id: build_launch_profile(
                definition, executable, session_id, {"PATH": "/usr/bin"}
            )
        }
        if profiles
        else {}
    )
    return TmuxTerminal(
        gateway,
        {_PROJECT: executable.parent},
        {},
        startup_timeout=1,
        profile_factories=factories,
    )


def _tui(service: SessionService) -> RemoteAgentsTui:
    return RemoteAgentsTui(
        TuiContext(
            launcher=service,
            creator=object(),  # type: ignore[arg-type]
            profiles=(ProfileChoice("claude", True),),
            refresh_catalogue=lambda: _CATALOGUE,
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
            catalogue=_CATALOGUE,
        )
    )


async def test_the_terminal_gracefully_stops_a_session_the_service_launched(
    database: Path, executable: Path, tmp_path: Path
) -> None:
    gateway = RecordingGateway(tmp_path / "intents")
    service_connection = open_database(database)
    tui_connection = open_database(database)
    try:
        service = SessionService(
            SQLiteSessionStore(service_connection), _terminal(gateway, executable)
        )
        # A separate TmuxTerminal: its _session_profiles cache is empty for this session.
        tui_service = SessionService(
            SQLiteSessionStore(tui_connection), _terminal(gateway, executable)
        )

        launched = await service.launch(LaunchCommand(_PROJECT, _PROFILE, "service-1"))
        app = _tui(tui_service)

        async with app.run_test() as pilot:
            await pilot.press("ctrl+s")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await app._resolve_detail("graceful")
            await pilot.pause()

        assert gateway.keys, "the terminal resolved no profile and sent no keys"
        assert gateway.keys[0][0] == launched.session_id
        assert gateway.mutations == [("kill-session", f"ra-{launched.session_id}")]
        final = await service.list_sessions()
        assert [record.state for record in final] == [SessionState.ENDED]
    finally:
        service_connection.close()
        tui_connection.close()


async def test_the_profile_is_resolved_from_the_factories_not_a_process_local_cache(
    database: Path, executable: Path, tmp_path: Path
) -> None:
    """The stopping composition never saw the launch, so only the factories can answer."""
    gateway = RecordingGateway(tmp_path / "intents")
    service_connection = open_database(database)
    tui_connection = open_database(database)
    try:
        launching = _terminal(gateway, executable)
        stopping = _terminal(gateway, executable)
        service = SessionService(SQLiteSessionStore(service_connection), launching)
        tui_service = SessionService(SQLiteSessionStore(tui_connection), stopping)

        launched = await service.launch(LaunchCommand(_PROJECT, _PROFILE, "service-1"))

        assert launched.session_id in launching._session_profiles
        assert stopping._session_profiles == {}, "the stopping terminal must not have the cache"

        app = _tui(tui_service)
        async with app.run_test() as pilot:
            await app._show_detail(str(launched.session_id))
            await pilot.pause()
            await app._resolve_detail("graceful")
            await pilot.pause()

        assert gateway.keys and gateway.keys[0][0] == launched.session_id
    finally:
        service_connection.close()
        tui_connection.close()


async def test_a_stop_with_an_unresolvable_profile_fails_closed(
    database: Path, executable: Path, tmp_path: Path
) -> None:
    """DEC-006: never no-op and report success. Nothing may be sent to the pane."""
    gateway = RecordingGateway(tmp_path / "intents")
    service_connection = open_database(database)
    tui_connection = open_database(database)
    try:
        service = SessionService(
            SQLiteSessionStore(service_connection), _terminal(gateway, executable)
        )
        # This host curates no launch factory for the profile the session was started with.
        tui_service = SessionService(
            SQLiteSessionStore(tui_connection), _terminal(gateway, executable, profiles=False)
        )

        launched = await service.launch(LaunchCommand(_PROJECT, _PROFILE, "service-1"))
        app = _tui(tui_service)

        async with app.run_test() as pilot:
            await app._show_detail(str(launched.session_id))
            await pilot.pause()
            await app._resolve_detail("graceful")
            await pilot.pause()
            status = str(app.screen.query_one("#status").content)

        assert gateway.keys == [], "an unresolvable profile must send nothing to the pane"
        final = await service.list_sessions()
        assert [record.state for record in final] == [SessionState.RUNNING], (
            "the session must not be recorded as stopped when nothing stopped it"
        )
        assert "stopped" not in status.casefold()
    finally:
        service_connection.close()
        tui_connection.close()


async def test_force_stop_from_the_terminal_also_crosses_the_process_boundary(
    database: Path, executable: Path, tmp_path: Path
) -> None:
    gateway = RecordingGateway(tmp_path / "intents")
    service_connection = open_database(database)
    tui_connection = open_database(database)
    try:
        service = SessionService(
            SQLiteSessionStore(service_connection), _terminal(gateway, executable)
        )
        tui_service = SessionService(
            SQLiteSessionStore(tui_connection), _terminal(gateway, executable)
        )
        launched = await service.launch(LaunchCommand(_PROJECT, _PROFILE, "service-1"))

        app = _tui(tui_service)
        async with app.run_test() as pilot:
            await app._show_detail(str(launched.session_id))
            await pilot.pause()
            await app._resolve_detail("force")
            await pilot.pause()
            await app._resolve_force_confirm("force-confirm")
            await pilot.pause()

        final = await service.list_sessions()
        assert [record.state for record in final] == [SessionState.ENDED]
    finally:
        service_connection.close()
        tui_connection.close()


def test_the_two_compositions_share_only_the_database_and_the_gateway(
    executable: Path, tmp_path: Path
) -> None:
    """Guards the premise: if these ever became one object the tests above prove nothing."""
    gateway = RecordingGateway(tmp_path / "intents")
    first = _terminal(gateway, executable)
    second = _terminal(gateway, executable)

    assert first is not second
    assert first._session_profiles is not second._session_profiles
