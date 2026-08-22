"""The console follows session lifecycle through the surface's own choke points.

The composer is wired into exactly two moments — `load_sessions` (every list reload, which
every stop path ends in) and `open` (which shows the session) — so these tests drive those
moments over a recording console, without a console of any kind touching a session record.

**This file used to be about tabs.** Under the tab model each live session was linked into
the console as its own window, and a reload linked the new ones and unlinked the gone ones.
That mechanism is retired (Sub-plan 3, Task 2.4): the console is one window of three panes
and it shows a session by *exchanging* its left pane. What survives is the question those
tests were really asking — does the console notice what the other writer did, and does
opening a session reach that session — so that is what these ask now.

The composition half is proven at the seam `local_context` owns: console hosting wires the
capabilities, any other hosting wires none.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from backends import backend_for

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
from remote_agents.ports.console import HostedPane

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
    def __init__(self, panes=()) -> None:
        self.windows: list[tuple[int, SessionId | None]] = [(0, None)]
        self.panes = list(panes)
        self.calls: list[tuple] = []

    async def console_exists(self) -> bool:
        return True

    async def create_console(self, command: tuple[str, ...], cwd: Path) -> None:
        self.calls.append(("create_console",))

    async def install_console_binding(self, key: str) -> None:
        self.calls.append(("install_console_binding", key))

    async def pane_arrangement(self):
        return tuple(self.panes)

    async def swap_panes(self, source_pane: str, target_pane: str) -> None:
        self.calls.append(("swap", source_pane, target_pane))


class _Launcher:
    def __init__(self, records: list[SessionRecord]) -> None:
        self.records = records

    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return tuple(self.records)


def _context(launcher: _Launcher, composer: ConsoleComposer) -> TuiContext:
    async def open_in_console(session_id: str) -> None:
        await composer.show(SessionId.parse(session_id))

    return TuiContext(
        backend=backend_for(
            sessions=launcher,  # type: ignore[arg-type]
            projects=object(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_PROJECT,),
        ),
        profiles=(ProfileChoice("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        open_in_console=open_in_console,
        console_sync=composer.sync,
    )


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


def _console_pane(pane_id: str, slot: str) -> HostedPane:
    return HostedPane(None, True, 0, int(pane_id[1:]), pane_id, None, slot == "surface", slot)


async def test_opening_a_session_exchanges_it_into_the_consoles_left_pane() -> None:
    """What "open this session" means now: one exchange, and the surface goes to live in the
    agent's own window until it is swapped back."""
    console = RecordingConsole(
        panes=(
            _console_pane("%0", "surface"),
            _console_pane("%1", "sessions"),
            _console_pane("%2", "feed"),
            HostedPane(None, False, 0, 0, "%7", _SESSION),
        )
    )
    composer = ConsoleComposer(
        console, ("remote-agents", "tui"), Path("/tmp"), projects_command=("true",)
    )
    launcher = _Launcher([_record(SessionState.RUNNING)])
    app = RemoteAgentsTui(_context(launcher, composer))

    async with app.run_test() as pilot:
        await app._open_or_leave(str(_SESSION))
        await pilot.pause()

    assert console.calls == [("swap", "%7", "%0")]
    assert app.return_value is None, "showing a session never exits the surface"


async def test_a_reload_notices_the_session_the_other_writer_stopped() -> None:
    """The half of the old tab sync that the swap model still needs.

    The bot is a separate process with no composer (DEC-005), so when it stops the session
    the console is displaying, nothing tells the console. A list reload is where it finds out.
    """
    console = RecordingConsole(
        panes=(
            # The agent is displayed: its pane is in the console, the surface is parked in
            # the agent's own window.
            HostedPane(None, True, 0, 0, "%7", _SESSION),
            _console_pane("%1", "sessions"),
            _console_pane("%2", "feed"),
            HostedPane(_SESSION, False, 0, 0, "%0", None, True, "surface"),
        )
    )
    composer = ConsoleComposer(
        console, ("remote-agents", "tui"), Path("/tmp"), projects_command=("true",)
    )
    launcher = _Launcher([_record(SessionState.ENDED)])
    app = RemoteAgentsTui(_context(launcher, composer))

    async with app.run_test() as pilot:
        await app.load_sessions()
        await pilot.pause()

    assert ("swap", "%0", "%7") in console.calls, (
        "a session that ended while displayed must give the left slot back to the surface"
    )
