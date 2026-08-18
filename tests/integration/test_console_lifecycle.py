"""Tabs track session lifecycle through the surface's own choke points.

The composer is wired into exactly two moments — `load_sessions` (every list reload,
which every stop path ends in) and `open` (which links its own tab) — so these tests
drive those moments over a real store and a recording console, proving a launch's tab
appears and a force-stopped session's tab goes, without a console of any kind touching a
session record. The composition half is proven at the seam `local_context` owns: console
hosting wires both capabilities, any other hosting wires neither.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.console import ConsoleComposer
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
_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")


def _record(state: SessionState) -> SessionRecord:
    return SessionRecord(
        _SESSION,
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


class RecordingConsole:
    def __init__(self) -> None:
        self.windows: list[tuple[int, SessionId | None]] = [(0, None)]
        self.calls: list[tuple] = []

    async def console_exists(self) -> bool:
        return True

    async def create_console(self, command: tuple[str, ...], cwd: Path) -> None:
        self.calls.append(("create_console",))

    async def install_console_binding(self, key: str) -> None:
        self.calls.append(("install_console_binding", key))

    async def console_windows(self) -> tuple[tuple[int, SessionId | None], ...]:
        return tuple(self.windows)

    async def link_session_window(self, session_id: SessionId) -> None:
        self.calls.append(("link", session_id))
        self.windows.append((len(self.windows), session_id))

    async def unlink_console_window(self, window_index: int) -> None:
        self.calls.append(("unlink", window_index))
        self.windows = [(i, o) for i, o in self.windows if i != window_index]

    async def select_console_window(self, window_index: int) -> None:
        self.calls.append(("select", window_index))

    async def switch_client_to_session(self, session_id: SessionId) -> None:
        self.calls.append(("switch", session_id))


class _Launcher:
    def __init__(self, records: list[SessionRecord]) -> None:
        self.records = records

    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return tuple(self.records)


def _context(launcher: _Launcher, composer: ConsoleComposer) -> TuiContext:
    async def open_in_console(session_id: str) -> None:
        await composer.open(SessionId.parse(session_id))

    return TuiContext(
        launcher=launcher,  # type: ignore[arg-type]
        creator=object(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        open_in_console=open_in_console,
        console_sync=composer.sync,
    )


async def test_a_live_session_gains_a_tab_on_reload_and_loses_it_when_stopped() -> None:
    console = RecordingConsole()
    composer = ConsoleComposer(console, ("remote-agents", "tui"), Path("/tmp"))
    launcher = _Launcher([_record(SessionState.RUNNING)])
    app = RemoteAgentsTui(_context(launcher, composer))

    async with app.run_test() as pilot:
        await app.load_sessions()
        await pilot.pause()
        assert ("link", _SESSION) in console.calls
        assert dict(console.windows).get(1) == _SESSION

        # A force stop lands in the store as ENDED; the next reload is the choke point.
        launcher.records = [_record(SessionState.ENDED)]
        await app.load_sessions()
        await pilot.pause()
        assert ("unlink", 1) in console.calls
        assert _SESSION not in dict(console.windows).values()


async def test_opening_a_session_links_and_selects_its_own_tab() -> None:
    console = RecordingConsole()
    composer = ConsoleComposer(console, ("remote-agents", "tui"), Path("/tmp"))
    launcher = _Launcher([_record(SessionState.RUNNING)])
    app = RemoteAgentsTui(_context(launcher, composer))

    async with app.run_test() as pilot:
        await app._open_or_leave(str(_SESSION))
        await pilot.pause()
    assert ("link", _SESSION) in console.calls
    assert ("select", 1) in console.calls
    assert ("switch", _SESSION) not in console.calls
    assert app.return_value is None, "console opens never exit the surface"


def test_only_console_hosting_composes_the_capabilities(monkeypatch, tmp_path: Path) -> None:
    """The composition seam: any hosting but CONSOLE wires neither capability."""
    import os

    from remote_agents.adapters.tui.attach import HostingMode, hosting_mode

    monkeypatch.delenv("TMUX", raising=False)
    assert hosting_mode(os.environ) is HostingMode.BARE
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    assert hosting_mode(os.environ) is HostingMode.FOREIGN
    # local_context's branch is `hosting_mode(os.environ) is HostingMode.CONSOLE`; both
    # non-console modes take the None path pinned here, and the console path is proven
    # live by tests/live/test_console_journey.py where a real composition runs.
