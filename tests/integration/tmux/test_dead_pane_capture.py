"""The output of a dead pane is read whole, on a real tmux server.

When a pane dies, tmux moves the cursor to the bottom and writes its `Pane is dead` notice with
a linefeed, so the screen scrolls up one line, always. The visible screen alone therefore loses
the top line, and for an agent that printed one fatal error that line is all it said.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

from remote_agents.adapters.tmux.codec import exact_session_target
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner
from remote_agents.domain.models import SessionId


class _Gateway(TmuxGateway):
    """Addresses the session directly: these panes carry no managed mark, and finding a pane by
    its mark has its own tests."""

    async def _following_target(self, session_id: SessionId) -> str:
        return exact_session_target(f"ra-{session_id}")


async def _pane_dead(runner: AsyncTmuxRunner, socket: str, session: str) -> None:
    for _ in range(200):
        output = await runner.run(
            "tmux", "-L", socket, "display-message", "-p", "-t", f"={session}:", "#{pane_dead}"
        )
        if output.strip() == "1":
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the pane never died")


async def _session(runner: AsyncTmuxRunner, socket: str, command: str) -> SessionId:
    session_id = SessionId.new()
    session = f"ra-{session_id}"
    await runner.run(
        "tmux",
        "-L",
        socket,
        "-f",
        "/dev/null",
        "new-session",
        "-d",
        "-s",
        "pin",
        "-x",
        "80",
        "-y",
        "24",
        "sleep",
        "60",
    )
    await runner.run("tmux", "-L", socket, "set-option", "-g", "remain-on-exit", "on")
    await runner.run(
        "tmux",
        "-L",
        socket,
        "new-session",
        "-d",
        "-s",
        session,
        "-x",
        "80",
        "-y",
        "24",
        "sh",
        "-c",
        command,
    )
    return session_id


async def _kill(runner: AsyncTmuxRunner, socket: str) -> None:
    try:
        await runner.run("tmux", "-L", socket, "kill-server")
    except RuntimeError:
        pass
    (Path(f"/tmp/tmux-{os.getuid()}") / socket).unlink(missing_ok=True)


async def test_a_dead_pane_s_one_line_of_output_is_read(tmp_path: Path) -> None:
    socket = f"remote-agents-test-{uuid4().hex}"
    runner = AsyncTmuxRunner()
    gateway = _Gateway(socket, runner, intent_directory=tmp_path / "intents")
    try:
        session_id = await _session(runner, socket, "printf 'fatal: the one line\\n'; exit 1")
        await _pane_dead(runner, socket, f"ra-{session_id}")

        output = await gateway.capture_output(session_id)

        assert "fatal: the one line" in output
        assert "Pane is dead" in output
    finally:
        await _kill(runner, socket)


async def test_a_full_dead_pane_keeps_its_top_line_and_nothing_older(tmp_path: Path) -> None:
    """One line of history is what the notice pushed away, so the read is exactly what the
    screen held when the agent died, not the whole scrollback."""
    socket = f"remote-agents-test-{uuid4().hex}"
    runner = AsyncTmuxRunner()
    gateway = _Gateway(socket, runner, intent_directory=tmp_path / "intents")
    try:
        session_id = await _session(
            runner, socket, "i=1; while [ $i -le 40 ]; do echo line-$i; i=$((i+1)); done; exit 1"
        )
        await _pane_dead(runner, socket, f"ra-{session_id}")

        lines = (await gateway.capture_output(session_id)).splitlines()

        # 24 rows held line-18 to line-40 and the cursor's empty row when the agent died.
        assert lines[0] == "line-18" and "line-40" in lines and "line-17" not in lines
    finally:
        await _kill(runner, socket)


async def test_a_live_pane_reads_only_its_screen(tmp_path: Path) -> None:
    """Nothing was pushed away from a live pane, and a history line above its screen would be
    text the owner never saw there."""
    socket = f"remote-agents-test-{uuid4().hex}"
    runner = AsyncTmuxRunner()
    gateway = _Gateway(socket, runner, intent_directory=tmp_path / "intents")
    try:
        session_id = await _session(
            runner, socket, "i=1; while [ $i -le 40 ]; do echo line-$i; i=$((i+1)); done; sleep 30"
        )
        for _ in range(200):
            lines = (await gateway.capture_output(session_id)).splitlines()
            if "line-40" in lines:
                break
            await asyncio.sleep(0.05)

        assert "line-40" in lines and "line-17" not in lines
    finally:
        await _kill(runner, socket)
