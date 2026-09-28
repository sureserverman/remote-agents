"""A throwaway `CODEX_HOME` for the live drills that open a real Codex TUI.

**Short, because Codex 0.158.0 opens a Unix socket inside it.** The TUI runs through an app-server
daemon it starts per home, listening on `<home>/app-server-control/app-server-control.sock`. A home
under pytest's `tmp_path` put that path at 127-128 characters, past the 107 a socket address
holds, and Codex exited 1 ("path must be shorter than SUN_LEN") before drawing anything: every
Codex drill failed at its opening with an empty pane (measured 2026-09-28).

**Retired, because that daemon outlives the pane.** `codex app-server daemon stop` stops the server
and leaves its `pid-update-loop` running (measured), so whatever still runs from inside the home is
ended too. Linux only for that half: it reads `/proc`, and elsewhere the stop is all there is.

**Never raises.** With a turn still waiting on an approval the stop hung past 30 s (measured on the
activity-hooks drill), and the `TimeoutExpired` from this `finally` replaced the test's own result.
So the stop gets a short bound, and what is left is sent SIGTERM, then SIGKILL.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path


def short_codex_home() -> Path:
    """A new private directory directly under `/tmp`, for one drill's `CODEX_HOME`."""
    home = Path(tempfile.mkdtemp(prefix="ra-cx-", dir="/tmp"))
    home.chmod(0o700)
    return home


def _running_from(home: Path) -> list[int]:
    prefix = f"{home}/"
    found = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            if os.readlink(entry / "exe").startswith(prefix):
                found.append(int(entry.name))
        except OSError:
            continue
    return found


def _signal(pids: list[int], signum: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, signum)
        except OSError:
            pass


def retire_codex_home(home: Path) -> None:
    """Stop the daemon a drill's Codex started in `home`, end what it left, remove the home."""
    codex = shutil.which("codex")
    if codex is not None:
        try:
            subprocess.run(
                [codex, "app-server", "daemon", "stop"],
                env={**os.environ, "CODEX_HOME": str(home)},
                capture_output=True,
                timeout=5,
                check=False,
            )
        except subprocess.TimeoutExpired:
            pass
    _signal(_running_from(home), signal.SIGTERM)
    deadline = time.monotonic() + 5
    while _running_from(home) and time.monotonic() < deadline:
        time.sleep(0.1)
    _signal(_running_from(home), signal.SIGKILL)
    shutil.rmtree(home, ignore_errors=True)
