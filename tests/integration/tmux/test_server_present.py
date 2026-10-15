"""The terminal can say when its dedicated tmux server started, or that none is running.

The inventory deliberately answers "no server running" with zero panes, because for the
reconcile pass the two mean the same thing. They do not mean the same thing to the resume pass.
A server that is gone, or one that started after a session did, means that session's pane was
lost with its server -- a reboot, a power loss, a tmux crash -- even when something (the
console, say) has started a fresh server since. A server older than the session that is
missing only its pane means nothing was interrupted.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner


def _socket() -> str:
    return f"remote-agents-test-{uuid4().hex}"


async def _kill(socket: str) -> None:
    try:
        await AsyncTmuxRunner().run("tmux", "-L", socket, "kill-server")
    except RuntimeError:
        pass
    (Path(f"/tmp/tmux-{os.getuid()}") / socket).unlink(missing_ok=True)


async def test_a_running_server_reports_when_it_started_and_a_killed_one_reports_none(
    tmp_path: Path,
) -> None:
    socket = _socket()
    runner = AsyncTmuxRunner()
    gateway = TmuxGateway(socket, runner, intent_directory=tmp_path / "intents")
    before = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=1)
    try:
        await runner.run(
            "tmux", "-L", socket, "-f", "/dev/null", "new-session", "-d", "-s", "pin", "sleep", "30"
        )
        started = await gateway.server_started_at()
        assert started is not None
        assert before <= started <= datetime.now(UTC) + timedelta(seconds=1)

        await runner.run("tmux", "-L", socket, "kill-server")
        assert await gateway.server_started_at() is None
    finally:
        await _kill(socket)


async def test_a_socket_that_never_existed_has_no_server(tmp_path: Path) -> None:
    gateway = TmuxGateway(_socket(), AsyncTmuxRunner(), intent_directory=tmp_path / "intents")
    assert await gateway.server_started_at() is None


async def test_the_fake_terminal_answers_what_it_is_told() -> None:
    from remote_agents.adapters.tmux.fake import FakeTerminal

    terminal = FakeTerminal()
    assert await terminal.server_started_at() is not None
    terminal.server_started = None
    assert await terminal.server_started_at() is None
