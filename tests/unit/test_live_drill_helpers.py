"""The helpers the native drills use to say "one daemon", which no host-side run can drive."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import codex_home
import pytest
from native_isolation import sharing_daemon

_NEEDS_PROCFS = pytest.mark.skipif(
    not Path("/proc/uptime").is_file(), reason="process identity reads Linux procfs"
)


def test_sharing_a_daemon_removes_only_the_pid_namespace() -> None:
    command = ("bwrap", "--unshare-pid", "--ro-bind", "/", "/", "--die-with-parent", "--", "codex")
    assert sharing_daemon(command) == (
        "bwrap",
        "--ro-bind",
        "/",
        "/",
        "--die-with-parent",
        "--",
        "codex",
    )


def test_every_codex_drill_that_counts_daemons_runs_without_its_own_namespace() -> None:

    live = Path(__file__).resolve().parents[1] / "live"
    for name in (
        "test_rollover_live.py",
        "test_restart_resume_live.py",
        "test_codex_tool_shell_identity.py",
    ):
        source = (live / name).read_text(encoding="utf-8")
        assert "sharing_daemon(" in source, name


@_NEEDS_PROCFS
def test_counting_daemons_reads_the_processes_every_time() -> None:
    """Each drill adds every reading up, so a remembered first one would hide a replaced daemon."""
    home = codex_home.short_codex_home()
    daemon = None
    try:
        assert codex_home.managed_daemons(home) == set()
        daemon = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", "--managed-daemon"],
            env={"CODEX_HOME": str(home)},
        )
        deadline = time.monotonic() + 10
        while not codex_home.managed_daemons(home):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert codex_home.managed_daemons(home) == {daemon.pid}
    finally:
        if daemon is not None:
            daemon.kill()
            daemon.wait()
        codex_home.retire_codex_home(home)


@_NEEDS_PROCFS
def test_a_process_nobody_can_read_does_not_stop_a_count() -> None:
    """A drill polls this, so one unreadable app must not fail a run the way cleanup refuses."""
    home = codex_home.short_codex_home()
    unreadable = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import ctypes, time; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0); "
            "print('ready', flush=True); time.sleep(30)",
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert unreadable.stdout.readline().strip() == b"ready"
        assert codex_home.managed_daemons(home) == set()
    finally:
        unreadable.kill()
        unreadable.wait()
        codex_home.retire_codex_home(home)
