"""A surface fitting its own pane never unzooms the console, on a real tmux server.

`resize-pane` unzooms the window it lands in, and a folded console is a zoomed one. The limits
pane fits its own height whenever its width changes, which a fold's slide does, so its fit used
to land after the fold's zoom and undo it (found by
`tests/e2e/test_console_panes.py::test_ten_toggles_at_a_real_client_leave_every_pane_idle`,
2026-11-20, 2 runs in 3). These cases drive `TmuxGateway.fit_pane_height` itself.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner


def _socket() -> str:
    return f"remote-agents-test-{uuid4().hex}"


async def _stop(runner: AsyncTmuxRunner, socket: str) -> None:
    try:
        await runner.run("tmux", "-L", socket, "kill-server")
    except RuntimeError:
        pass
    (Path(f"/tmp/tmux-{os.getuid()}") / socket).unlink(missing_ok=True)


async def _three_panes(runner: AsyncTmuxRunner, socket: str) -> tuple[str, str]:
    """A left pane and a right column of two, as the console has; returns (left, right top)."""
    tmux = ("tmux", "-L", socket)
    await runner.run(
        *tmux, "-f", "/dev/null", "new-session", "-d", "-s", "fit", "-x", "120", "-y", "40"
    )
    await runner.run(*tmux, "split-window", "-h", "-t", "fit")
    await runner.run(*tmux, "split-window", "-v", "-t", "fit:0.1")
    left = (await runner.run(*tmux, "display-message", "-p", "-t", "fit:0.0", "#{pane_id}")).strip()
    right = (
        await runner.run(*tmux, "display-message", "-p", "-t", "fit:0.1", "#{pane_id}")
    ).strip()
    return left, right


async def _read(runner: AsyncTmuxRunner, socket: str, pane: str, fmt: str) -> str:
    return (
        await runner.run("tmux", "-L", socket, "display-message", "-p", "-t", pane, fmt)
    ).strip()


async def test_a_fit_while_the_console_is_folded_leaves_it_folded(tmp_path: Path) -> None:
    socket = _socket()
    runner = AsyncTmuxRunner()
    gateway = TmuxGateway(socket, runner, intent_directory=tmp_path / "intents")
    try:
        left, right = await _three_panes(runner, socket)
        await runner.run("tmux", "-L", socket, "resize-pane", "-Z", "-t", left)
        assert await _read(runner, socket, left, "#{window_zoomed_flag}") == "1"

        await gateway.fit_pane_height(right, 7)

        assert await _read(runner, socket, left, "#{window_zoomed_flag}") == "1", (
            "a surface's own fit unzoomed the folded console"
        )
    finally:
        await _stop(runner, socket)


async def test_a_fit_on_an_unfolded_console_still_resizes_the_pane(tmp_path: Path) -> None:
    """The guard skips only a zoomed window: a fit that never resized would pass the case
    above vacuously."""
    socket = _socket()
    runner = AsyncTmuxRunner()
    gateway = TmuxGateway(socket, runner, intent_directory=tmp_path / "intents")
    try:
        _left, right = await _three_panes(runner, socket)

        await gateway.fit_pane_height(right, 7)

        assert await _read(runner, socket, right, "#{pane_height}") == "7"
        assert await _read(runner, socket, right, "#{window_zoomed_flag}") == "0"
    finally:
        await _stop(runner, socket)
