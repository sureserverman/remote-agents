"""Two surfaces take turns arranging the console, and neither waits on the other forever.

The lock exists because the bot's stop path now steps the console aside before it destroys a
pane, which is what makes a phone stop hand the projects list back immediately instead of
leaving the console a pane short until its next reload. That put a *second process* on the
console's panes, and DEC-005's premise had been that there was only ever one.

What two processes deciding from the same stale reading produce is not cosmetic:
`ConsoleComposer._restore_stale_display` spells it out — the remembered pane ids come to name
different places, and the swap puts a live agent's pane into a stranger's window.
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from pathlib import Path

import pytest

from remote_agents.application.console_lock import ConsoleArrangementLock, ConsoleBusy


async def test_it_serialises_two_callers_in_one_process() -> None:
    """The guarantee the plain `asyncio.Lock` already gave, kept."""
    lock = ConsoleArrangementLock()
    order: list[str] = []

    async def arrange(name: str) -> None:
        async with lock:
            order.append(f"{name} in")
            await asyncio.sleep(0.01)
            order.append(f"{name} out")

    await asyncio.gather(arrange("a"), arrange("b"))

    assert order in (
        ["a in", "a out", "b in", "b out"],
        ["b in", "b out", "a in", "a out"],
    ), order


async def test_it_excludes_another_process_holding_the_same_file(tmp_path: Path) -> None:
    """The half an `asyncio.Lock` cannot give, proved against a real second process.

    A thread would not prove it: `flock` conflicts between *open file descriptions*, and the
    question is whether a genuinely separate process is kept out. So one is spawned, made to
    take the lock, and this one is given a deadline short enough to fail while it is held.
    """
    path = tmp_path / "console.lock"
    ready, done = tmp_path / "ready", tmp_path / "done"
    holder = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        textwrap.dedent(
            f"""
            import fcntl, time, pathlib
            handle = open({str(path)!r}, "a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            pathlib.Path({str(ready)!r}).write_text("held")
            while not pathlib.Path({str(done)!r}).exists():
                time.sleep(0.01)
            """
        ),
    )
    try:
        for _ in range(500):
            if ready.exists():
                break
            await asyncio.sleep(0.01)
        assert ready.exists(), "the second process never took the lock"

        with pytest.raises(ConsoleBusy):
            async with ConsoleArrangementLock(path, timeout=0.2):
                raise AssertionError("the lock was taken while another process held it")
    finally:
        done.write_text("go")
        await holder.wait()

    # And it is available the moment that process lets go — the wait is bounded, not poisoned.
    async with ConsoleArrangementLock(path, timeout=1.0):
        pass


async def test_the_in_process_lock_is_released_when_the_file_half_gives_up(
    tmp_path: Path,
) -> None:
    """The failure mode a `try` in the wrong place produces: a console that locks itself out.

    `__aenter__` takes the in-process lock first, so a `ConsoleBusy` from the file half must
    hand it back. Left held, the *next* arrangement in this process would wait on a lock
    nothing will ever release — and every later one behind it.
    """
    path = tmp_path / "console.lock"
    lock = ConsoleArrangementLock(path, timeout=0.05)
    handle = path.open("a+")
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        with pytest.raises(ConsoleBusy):
            async with lock:
                pass
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    async with lock:
        pass


async def test_a_lock_file_it_cannot_create_degrades_to_this_process_alone(
    tmp_path: Path,
) -> None:
    """DEC-006's rule reaches the lock too: a display that cannot coordinate still displays.

    The console is presentation, so a state directory this process cannot write is worth a
    warning and the guarantee this project had before — never a console that refuses to
    arrange itself at all.
    """
    closed = tmp_path / "closed"
    closed.mkdir(mode=0o500)
    lock = ConsoleArrangementLock(closed / "nested" / "console.lock", timeout=0.05)
    try:
        async with lock:
            pass
        async with lock:
            pass
    finally:
        closed.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only directory")
def test_the_unwritable_case_is_actually_unwritable(tmp_path: Path) -> None:
    """Guards the test above from passing because the directory was writable after all."""
    closed = tmp_path / "closed"
    closed.mkdir(mode=0o500)
    try:
        with pytest.raises(OSError):
            (closed / "nested").mkdir()
    finally:
        closed.chmod(0o700)
