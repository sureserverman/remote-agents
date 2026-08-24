"""Transient supervisor probe that creates one isolated tmux session and then waits.

Named for the supervisor rather than for systemd since Stage 2 gave the concept two
implementations: Stage 3 drives this same probe from a launchd agent, where the survival
property it exists to demonstrate is `AbandonProcessGroup` rather than `KillMode=process`.
"""

from __future__ import annotations

import asyncio

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
        and await _run_tmux("new-session", "-d", "-s", _SESSION, "/usr/bin/sleep", "3600") != 0
    ):
        raise RuntimeError("tmux lifecycle probe failed")
    if not await _session_is_ready():
        raise RuntimeError("tmux lifecycle probe did not become ready")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
