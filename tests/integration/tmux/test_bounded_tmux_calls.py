"""Every tmux call the service makes ends within a bound, as a failure if tmux never answers.

On 2026-11-19 (v0.70.3) one `send-keys` waited 3.5 hours on a search prompt and held the
service's operation lock the whole time. `tmux wait-for` on a channel nobody signals is the
same shape with nothing else involved: a real tmux client that never returns.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from uuid import uuid4

import pytest

from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")


@pytest.fixture
def socket():
    name = f"remote-agents-test-{uuid4().hex}"
    subprocess.run(("tmux", "-L", name, "new-session", "-d", "-s", "idle", "sleep 600"), check=True)
    try:
        yield name
    finally:
        subprocess.run(("tmux", "-L", name, "kill-server"), capture_output=True)


async def test_a_tmux_call_that_never_returns_fails_within_its_bound(socket: str) -> None:
    runner = AsyncTmuxRunner(timeout=0.5)
    started = time.monotonic()

    with pytest.raises(RuntimeError, match=r"timed out after 0\.5s: wait-for"):
        await asyncio.wait_for(runner.run("tmux", "-L", socket, "wait-for", "never"), 10.0)

    assert time.monotonic() - started < 5.0
    # The killed client is gone, so nothing is left waiting on the channel.
    clients = subprocess.run(
        ("pgrep", "-f", f"tmux -L {socket} wait-for"), capture_output=True, text=True
    )
    assert clients.stdout.strip() == ""


async def test_a_call_inside_its_bound_is_unchanged(socket: str) -> None:
    runner = AsyncTmuxRunner(timeout=5.0)

    out = await runner.run("tmux", "-L", socket, "display-message", "-p", "#{session_name}")

    assert out.strip() == "idle"


async def test_the_failure_names_the_verb_and_never_the_text_it_carried(socket: str) -> None:
    runner = AsyncTmuxRunner(timeout=0.5)

    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(
            runner.run("tmux", "-L", socket, "wait-for", "a-private-prompt"), 10.0
        )

    assert "wait-for" in str(raised.value)
    assert "private" not in str(raised.value)
