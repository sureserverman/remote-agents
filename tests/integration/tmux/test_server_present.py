"""The terminal can say whether its dedicated tmux server is up at all.

The inventory deliberately answers "no server running" with zero panes, because for the
reconcile pass the two mean the same thing. They do not mean the same thing to the resume pass:
a server that is gone means the host lost every pane at once -- a reboot, a power loss, a tmux
crash -- while a server that is up with no managed panes means nothing was interrupted.
`server_present` keeps the difference the inventory folds away.
"""

from __future__ import annotations

import os
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


async def test_a_running_server_is_present_and_a_killed_one_is_not(tmp_path: Path) -> None:
    socket = _socket()
    runner = AsyncTmuxRunner()
    gateway = TmuxGateway(socket, runner, intent_directory=tmp_path / "intents")
    try:
        await runner.run(
            "tmux", "-L", socket, "-f", "/dev/null", "new-session", "-d", "-s", "pin", "sleep", "30"
        )
        assert await gateway.server_present() is True

        await runner.run("tmux", "-L", socket, "kill-server")
        assert await gateway.server_present() is False
    finally:
        await _kill(socket)


async def test_a_socket_that_never_existed_is_not_present(tmp_path: Path) -> None:
    gateway = TmuxGateway(_socket(), AsyncTmuxRunner(), intent_directory=tmp_path / "intents")
    assert await gateway.server_present() is False


async def test_the_fake_terminal_answers_what_it_is_told() -> None:
    from remote_agents.adapters.tmux.fake import FakeTerminal

    terminal = FakeTerminal()
    assert await terminal.server_present() is True
    terminal.server_up = False
    assert await terminal.server_present() is False
