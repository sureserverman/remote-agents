"""A stand-in agent for live tmux drives, and the one honest way to ask if it has gone.

Shared rather than copied because both halves are load-bearing and both have already been
got wrong once. The profile is deliberately not a curated agent: proving that a pane moved,
or that a stop reached it, needs a process with a pty and a readiness banner, not Claude —
and depending on an agent binary would make the drive skip on hosts where the mechanism
works perfectly. `process_gone` is here for the opposite reason: the naive version of it
raced, and a second copy of a raced check is a second flake nobody attributes.

Lives in `tests/support` (on the pytest pythonpath) so a live module imports it rather than
redefining it — two definitions of "the stand-in agent" would drift, and the drift would
show up as one drive proving something slightly different from its sibling.
"""

from __future__ import annotations

import asyncio
import os

from process_state import process_state

from remote_agents.adapters.tmux.runtime import LaunchProfile

MARKER = "PANE-ADDRESSING-LIVE"


def probe_profile() -> LaunchProfile:
    """A stand-in agent: prints a readiness banner, then waits to be stopped.

    No curated profile is needed to prove addressing, and using one would make the test
    depend on an agent binary being installed. `C-c` is a real graceful sequence here —
    tmux gives the pane a pty, so the key reaches the foreground process group.
    """
    shell = "/bin/sh"
    return LaunchProfile(
        executable=shell,
        argv=(shell, "-c", f"echo {MARKER}; sleep 300"),
        environment={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        readiness_marker=MARKER,
        graceful_keys=("C-c",),
    )


async def process_gone(pid: str, *, within: float = 10.0) -> bool:
    """Whether a pid has stopped running, polled — and a zombie counts as stopped.

    The pane reporting `pane_dead` is tmux noticing the exit; the process leaving the table
    is its parent reaping it, and those are not the same instant. A bare existence check
    raced the second one. A zombie is an exited process by every meaning these tests care
    about, so it is read from the status rather than from the process merely being listed.

    The status came from `/proc/<pid>/stat` until the two-OS matrix pointed out that macOS has
    no `/proc`; `process_state` asks `ps` instead, which answers on both.
    """
    deadline = asyncio.get_running_loop().time() + within
    while asyncio.get_running_loop().time() < deadline:
        state = process_state(pid)
        if state is None or state == "Z":
            return True
        await asyncio.sleep(0.1)
    return False
