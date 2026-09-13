"""`JsonRpcProcess`: the one transport every Codex child speaks through.

Driven against a stub child rather than `codex`, so nothing here needs the agent installed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from remote_agents.adapters.agents.protocols import JsonRpcProcess


def _stub(tmp_path: Path, body: str) -> tuple[str, ...]:
    script = tmp_path / "stub.py"
    script.write_text(body, encoding="utf-8")
    return (sys.executable, str(script))


#: Answers `initialize`, then ignores SIGTERM and stdin EOF: a child only `kill()` reclaims.
_STUBBORN = """\
import json, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
line = sys.stdin.readline()
message = json.loads(line)
sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}) + "\\n")
sys.stdout.flush()
time.sleep(600)
"""


async def test_child_pid_names_the_running_child_and_nothing_once_closed(tmp_path: Path) -> None:
    client = JsonRpcProcess(_stub(tmp_path, _STUBBORN))
    assert client.child_pid is None
    await client._ensure_started()
    pid = client.child_pid
    assert isinstance(pid, int) and pid > 0
    await client.close()
    assert client.child_pid is None


async def test_close_kills_the_child_when_it_is_cancelled_mid_wait(tmp_path: Path) -> None:
    """A bounded caller that gives up on `close()` must not leave the child running.

    `close()` lets go of its reference before waiting for the exit, so a cancellation that
    arrived during the wait used to orphan a live process nothing could reclaim.
    """
    client = JsonRpcProcess(_stub(tmp_path, _STUBBORN))
    await client._ensure_started()
    process = client._process
    assert process is not None
    closing = asyncio.create_task(client.close())
    await asyncio.sleep(0.2)
    closing.cancel()
    try:
        await closing
    except asyncio.CancelledError:
        pass
    await asyncio.wait_for(process.wait(), timeout=5)
    assert process.returncode is not None, "the child was killed on the way out"
    assert client.child_pid is None
