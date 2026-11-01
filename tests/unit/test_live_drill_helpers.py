"""The helpers the native drills use to say "one daemon", which no host-side run can drive."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import codex_home
import pytest
from native_isolation import sharing_daemon


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
    from pathlib import Path

    live = Path(__file__).resolve().parents[1] / "live"
    for name in (
        "test_rollover_live.py",
        "test_restart_resume_live.py",
        "test_codex_tool_shell_identity.py",
    ):
        source = (live / name).read_text(encoding="utf-8")
        assert "sharing_daemon(" in source, name


def test_counting_daemons_reads_the_processes_every_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each drill adds every reading up, so a remembered first one would hide a replaced daemon."""
    daemon = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "--managed-daemon"]
    )
    try:
        arguments = Path(f"/proc/{daemon.pid}/cmdline")
        deadline = time.monotonic() + 10
        while b"--managed-daemon" not in arguments.read_bytes():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        running = [[], [daemon.pid]]
        monkeypatch.setattr(codex_home, "_running_from", lambda home: running.pop(0))
        assert codex_home.managed_daemons(tmp_path) == set()
        assert codex_home.managed_daemons(tmp_path) == {daemon.pid}
    finally:
        daemon.kill()
        daemon.wait()
