"""Whether a process is still running, on a host that may not have `/proc`.

Every process-liveness check in this suite used to read `/proc/<pid>`, which exists on Linux
and not on macOS. The consequence was worse than a failure, because it was two different
things at once: the one *positive* assertion failed outright on a Mac, while the *negative*
ones passed vacuously -- a path that can never exist is always absent, so "the agent did not
outlive the stop" was true on macOS no matter what the agent did.

Nothing could have caught that before now. The suite had only ever run on one Ubuntu
workstation, and a vacuous pass is indistinguishable from a real one in every artifact a
single-platform run produces. The two-OS matrix is what surfaced it, on its first execution.
"""

from __future__ import annotations

import os
import subprocess
import time


def is_running(pid: int | str) -> bool:
    """Whether this pid still names a process the host has not reaped.

    `os.kill(pid, 0)` rather than a `/proc` lookup: it is POSIX, so it asks both supported
    platforms the same question instead of asking Linux a question and macOS nothing.

    `PermissionError` is a **yes**. It means the process exists and belongs to another user,
    which answers what was asked more strongly than a bare success does -- and swallowing it
    as a "no" would reintroduce a false negative on exactly the shared hosts where it matters.
    """
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_state(pid: int | str) -> str | None:
    """This pid's single-letter process state, or `None` once it has gone.

    `ps -o state=` is the portable spelling of the third field of `/proc/<pid>/stat`. macOS
    appends BSD flags to the value where Linux gives a bare letter -- measured on both, an
    ordinary sleeping process reads `S` on Linux and `SN  ` on macOS -- so only the first
    character carries the state, and returning more would hand callers a value that means
    something different on each platform.
    """
    completed = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    state = completed.stdout.strip()
    return state[0] if state else None


def reaped(pid: int | str, *, within: float = 10.0) -> bool:
    """Whether a pid has stopped running, polled -- and a zombie counts as stopped.

    A bare existence check races: the child exiting and its parent reaping it are not the same
    instant, and in between the process is still listed. `Z` is that window, and a zombie is an
    exited process by every meaning these tests have for the word.
    """
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        state = process_state(pid)
        if state is None or state == "Z":
            return True
        time.sleep(0.05)
    return False
