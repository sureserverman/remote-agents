"""One cross-process lock per session serialises every key-sending path (BL-056).

Three things type into a managed pane: the stop sequence, the Remote Control toggle, and now the
prompt relay. They run in two processes (the bot's service and the local surface), so an
`asyncio.Lock` would order one process against itself and nothing else, and two sequences sent at
once would interleave keystroke by keystroke in one pane. These cases drive real subprocesses.
"""

from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.key_lock import KeysBusy, SessionKeyLock
from remote_agents.domain.models import SessionId

_GATEWAY = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "remote_agents"
    / "adapters"
    / "tmux"
    / "gateway.py"
)

_WRITER = textwrap.dedent(
    """
    import asyncio, sys, time
    from pathlib import Path
    from remote_agents.adapters.tmux.key_lock import SessionKeyLock
    from remote_agents.domain.models import SessionId

    async def main(directory, session, log, name):
        async with SessionKeyLock(Path(directory), SessionId.parse(session), timeout=20):
            for key in range(3):
                with open(log, "a") as handle:
                    handle.write(f"{name}{key}\\n")
                time.sleep(0.15)

    asyncio.run(main(*sys.argv[1:]))
    """
)


def _spawn(directory: Path, session: SessionId, log: Path, name: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _WRITER, str(directory), str(session), str(log), name]
    )


def test_two_processes_sending_keys_to_one_session_never_interleave(tmp_path: Path) -> None:
    session = SessionId.new()
    log = tmp_path / "keys.log"
    writers = [_spawn(tmp_path / "locks", session, log, name) for name in ("a", "b")]
    for writer in writers:
        assert writer.wait(timeout=30) == 0

    written = log.read_text().split()
    assert sorted(written) == ["a0", "a1", "a2", "b0", "b1", "b2"]
    first, second = written[0][0], written[3][0]
    assert written == [f"{first}{n}" for n in range(3)] + [f"{second}{n}" for n in range(3)], (
        f"two senders' keys interleaved in one pane: {written}"
    )


def test_two_sessions_do_not_wait_for_each_other(tmp_path: Path) -> None:
    held = SessionId.new()
    other = SessionId.new()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _WRITER.replace("time.sleep(0.15)", "time.sleep(1.0)"),
            str(tmp_path / "locks"),
            str(held),
            str(tmp_path / "held.log"),
            "h",
        ]
    )
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "held.log").exists() and time.monotonic() < deadline:
            time.sleep(0.02)

        async def take_the_other() -> float:
            started = time.monotonic()
            async with SessionKeyLock(tmp_path / "locks", other, timeout=5):
                return time.monotonic() - started

        assert asyncio.run(take_the_other()) < 0.5
    finally:
        holder.wait(timeout=30)


def test_a_wedged_holder_costs_a_bounded_wait_and_a_named_failure(tmp_path: Path) -> None:
    session = SessionId.new()

    async def contend() -> None:
        async with SessionKeyLock(tmp_path / "locks", session, timeout=5):
            # A second, separate claim on the same session from this same process: `flock`
            # conflicts between open file descriptions, so it waits exactly as another process
            # would -- and must give up rather than hang.
            with pytest.raises(KeysBusy):
                async with SessionKeyLock(tmp_path / "locks", session, timeout=0.2):
                    pass

    asyncio.run(contend())


def test_without_a_lock_directory_the_lock_still_serialises_this_process() -> None:
    session = SessionId.new()
    order: list[str] = []

    async def sender(name: str) -> None:
        async with SessionKeyLock(None, session):
            for key in range(3):
                order.append(f"{name}{key}")
                await asyncio.sleep(0.01)

    async def both() -> None:
        await asyncio.gather(sender("a"), sender("b"))

    asyncio.run(both())
    assert order in (["a0", "a1", "a2", "b0", "b1", "b2"], ["b0", "b1", "b2", "a0", "a1", "a2"])


def _typing_constants(tree: ast.AST) -> list[ast.Constant]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and node.value in ("send-keys", "paste-buffer", "load-buffer")
    ]


def _locked_regions(tree: ast.AST) -> list[ast.AsyncWith]:
    regions = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncWith):
            for item in node.items:
                call = item.context_expr
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "_keys_for"
                ):
                    regions.append(node)
    return regions


def test_every_key_sending_argv_in_the_gateway_is_built_under_the_session_lock() -> None:
    """Parsed, not trusted: a new method that types into a pane must take the lock to exist."""
    tree = ast.parse(_GATEWAY.read_text(encoding="utf-8"))
    constants = _typing_constants(tree)
    assert constants, "the gateway sends no keys at all, so this check would be vacuous"
    inside = {
        id(constant) for region in _locked_regions(tree) for constant in _typing_constants(region)
    }
    outside = [constant.lineno for constant in constants if id(constant) not in inside]

    assert not outside, f"gateway.py builds a key-sending argv outside the lock at lines {outside}"
