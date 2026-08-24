"""Transient supervisor probe that creates one isolated tmux session and then waits.

Named for the supervisor rather than for systemd since Stage 2 gave the concept two
implementations: Stage 3 drives this same probe from a launchd agent, where the survival
property it exists to demonstrate is `AbandonProcessGroup` rather than `KillMode=process`.
"""

from __future__ import annotations

import asyncio
import pathlib
import shutil


def _sleep_command() -> str:
    """Where `sleep` actually lives, which is not the same answer on both platforms.

    `/usr/bin/sleep` on Linux; **`/bin/sleep` on macOS, where `/usr/bin/sleep` does not exist
    at all.** Hardcoding the Linux path made this probe unable to run under launchd, and the
    failure was quiet in the worst way: `tmux new-session -d` still exits 0, because the
    session is created and only *then* does its pane command fail to exec. The server frees the
    session and exits, so every later `has-session` answers "no server running" and the probe
    reports that tmux never became ready -- pointing at tmux, which was fine.

    Found by running the drill on real hardware rather than by reading the code, which is what
    Stage 3 exists for. `tests/live/test_pane_identity.py` already resolved this same
    difference for its own sleep; this is the sibling that was missed.

    Resolved through `PATH` first, because both `/bin` and `/usr/bin` are in `_PATH_STDPATH` --
    the bare environment launchd hands a job -- so this works in exactly the context that
    exposed the bug. The absolute candidates are the fallback for a caller with no usable PATH.
    """
    found = shutil.which("sleep")
    if found is not None:
        return found
    for candidate in ("/bin/sleep", "/usr/bin/sleep"):
        if pathlib.Path(candidate).is_file():
            return candidate
    raise RuntimeError("no sleep executable found for the tmux lifecycle probe")


_SOCKET = "remote-agents-test-service"
_SESSION = "ra-service-probe"


async def _run_tmux(*arguments: str) -> int:
    process = await asyncio.create_subprocess_exec("tmux", "-L", _SOCKET, *arguments)
    return await process.wait()


async def _session_is_ready() -> bool:
    for _ in range(50):
        if await _run_tmux("has-session", "-t", f"={_SESSION}") == 0:
            return True
        await asyncio.sleep(0.01)
    return False


async def main() -> None:
    """Start the exact harmless session once; the supervisor stopping this leaves it alive."""
    if (
        not await _session_is_ready()
        and await _run_tmux("new-session", "-d", "-s", _SESSION, _sleep_command(), "3600") != 0
    ):
        raise RuntimeError("tmux lifecycle probe failed")
    if not await _session_is_ready():
        raise RuntimeError("tmux lifecycle probe did not become ready")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
