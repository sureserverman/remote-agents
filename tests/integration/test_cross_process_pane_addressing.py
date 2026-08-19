"""Two independently composed terminals agree about a displaced session, and either stops it.

DEC-006 says a session's stop must not depend on the process that launched it, and DEC-005
accepts two writers over one store. Pane addressing is what makes both survive the console:
the process doing the stopping never saw the launch, never saw the swap, and holds no
memory of where the pane went — it resolves the identity against the server and acts on
whatever it finds, which is the agent.

Driven against **real tmux** rather than a double, deliberately. A fake gateway is written
by the same hand as the code under test, so it agrees with it about the thing most worth
doubting — which session target names which pane. The failure this guards is exactly a
disagreement with tmux, so tmux is the only witness worth calling. The compositions are
independent in the way `test_cross_process_tui.py` establishes: separate database
connections, separate `TmuxTerminal` instances with their own process-local profile caches.
Only the database file and the tmux server are shared, which is what two real processes
share.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.application.commands import ForceStopCommand, LaunchCommand
from remote_agents.application.reconcile import reconcile
from remote_agents.application.services import SessionService
from remote_agents.domain.models import ProfileId, ProjectId, SessionState

_PROFILE = ProfileId("claude")
_PROJECT = ProjectId("opaque-editor")
_MARKER = "CROSS-PROCESS-LIVE"


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "sessions.sqlite3"


def probe_profile() -> LaunchProfile:
    shell = "/bin/sh"
    return LaunchProfile(
        executable=shell,
        argv=(shell, "-c", f"echo {_MARKER}; sleep 300"),
        environment={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        readiness_marker=_MARKER,
        graceful_keys=("C-c",),
    )


def terminal(socket: str, workspace: Path) -> TmuxTerminal:
    """One composition, with its own gateway object and its own profile cache."""
    return TmuxTerminal(
        TmuxGateway(socket, AsyncTmuxRunner(), intent_directory=workspace / "intents"),
        {_PROJECT: workspace},
        {_PROFILE: probe_profile()},
        startup_timeout=15,
    )


async def displace(runner: AsyncTmuxRunner, base: tuple[str, ...], session_id) -> str:
    """Swap the agent's pane into the console's left slot, re-reading the slot each time."""
    agent = (
        await runner.run(*base, "list-panes", "-t", f"ra-{session_id}:", "-F", "#{pane_id}")
    ).strip()
    slot = (
        (await runner.run(*base, "list-panes", "-t", "ra-console:", "-F", "#{pane_id}"))
        .splitlines()[0]
        .strip()
    )
    await runner.run(*base, "swap-pane", "-s", agent, "-t", slot)
    return agent


async def test_either_terminal_can_stop_a_session_whose_pane_the_other_displaced(
    database: Path, tmp_path: Path
) -> None:
    # Unique per run: a socket named for the process would collide with a server a
    # crashed earlier run left behind, and the failure would look like a code defect.
    socket = f"remote-agents-test-{uuid4().hex}"
    runner = AsyncTmuxRunner()
    base = ("tmux", "-L", socket)
    launching_connection = open_database(database)
    stopping_connection = open_database(database)
    launching_terminal = terminal(socket, tmp_path)
    stopping_terminal = terminal(socket, tmp_path)
    try:
        await launching_terminal._gateway.create_console(("sleep", "600"), tmp_path)
        await runner.run(*base, "split-window", "-d", "-t", "ra-console:", "sleep", "600")

        launching = SessionService(SQLiteSessionStore(launching_connection), launching_terminal)
        stopping = SessionService(SQLiteSessionStore(stopping_connection), stopping_terminal)

        record = await launching.launch(LaunchCommand(_PROJECT, _PROFILE, "displaced"))
        assert record.state is SessionState.RUNNING

        # The console takes the agent's pane. Neither service is told.
        agent_pane = await displace(runner, base, record.session_id)

        # Both compositions still see one live session, and both see it in the console —
        # provenance the *other* process never told them, read from the server itself.
        for composition in (launching_terminal, stopping_terminal):
            observations = await composition.managed_observations()
            assert [item.session_id for item in observations] == [record.session_id]
            assert observations[0].live is True
            assert observations[0].host_session == "ra-console"

        # And they reconcile it identically: displaced is running, not gone.
        stored = await launching.list_sessions()
        verdicts = {
            name: reconcile(stored, await composition.managed_observations())
            for name, composition in (
                ("launching", launching_terminal),
                ("stopping", stopping_terminal),
            )
        }
        assert verdicts["launching"] == verdicts["stopping"]
        assert [item.state for item in verdicts["stopping"]] == [SessionState.RUNNING]

        # The process that never launched it, and never saw it move, stops it.
        agent_pid = (
            await runner.run(*base, "display-message", "-p", "-t", agent_pane, "#{pane_pid}")
        ).strip()
        await stopping.force_stop(ForceStopCommand(record.session_id))

        for _ in range(50):
            if (
                agent_pane
                not in (await runner.run(*base, "list-panes", "-a", "-F", "#{pane_id}")).split()
            ):
                break
            await asyncio.sleep(0.1)
        assert (
            agent_pane
            not in (await runner.run(*base, "list-panes", "-a", "-F", "#{pane_id}")).split()
        ), "the stop did not reach the displaced pane"
        assert not Path(f"/proc/{agent_pid}").exists(), "the agent outlived a cross-process stop"

        # The launching process, reading the same store, agrees the session is over.
        final = await launching.list_sessions()
        assert [item.state for item in final] == [SessionState.ENDED]

        # And the console it was displayed in is untouched (DEC-006, other direction).
        assert await launching_terminal._gateway.console_exists() is True
    finally:
        launching_connection.close()
        stopping_connection.close()
        try:
            await runner.run(*base, "kill-server")
        except RuntimeError:
            pass
        (Path(f"/tmp/tmux-{os.getuid()}") / socket).unlink(missing_ok=True)


def test_the_two_compositions_share_only_the_database_and_the_server(tmp_path: Path) -> None:
    """Guards the premise: if these ever became one object the test above proves nothing."""
    first = terminal("remote-agents-test-premise", tmp_path)
    second = terminal("remote-agents-test-premise", tmp_path)

    assert first is not second
    assert first._gateway is not second._gateway
    assert first._session_profiles is not second._session_profiles
