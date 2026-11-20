"""The helpers the native drills use to say "one daemon", which no host-side run can drive."""

from __future__ import annotations

import shutil
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
        "test_codex_shared_daemon_attribution.py",
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


def _bwrap_works() -> bool:
    if shutil.which("bwrap") is None:
        return False
    probe = subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "true"], capture_output=True, check=False
    )
    return probe.returncode == 0


_NEEDS_BWRAP = pytest.mark.skipif(not _bwrap_works(), reason="BLOCKED: bwrap cannot run here")


@pytest.fixture
def owner_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A made-up owner whose OpenCode is logged in, so no test reads the real login."""
    home = tmp_path / "owner"
    login = home / ".local" / "share" / "opencode" / "auth.json"
    login.parent.mkdir(parents=True)
    login.write_text('{"made-up": "login"}\n')
    login.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    return home


@_NEEDS_BWRAP
def test_opencode_sees_the_owner_login_read_only_over_a_placeholder(owner_home, tmp_path) -> None:
    from native_isolation import opencode_command

    root = tmp_path / "drill"
    data_home = root / "data"
    login = owner_home / ".local" / "share" / "opencode" / "auth.json"
    command = opencode_command(Path("/bin/sh"), root, data_home)

    placeholder = data_home / "opencode" / "auth.json"
    assert placeholder.is_file() and not placeholder.is_symlink()
    assert placeholder.stat().st_size == 0
    mounts = [command[i : i + 3] for i, part in enumerate(command) if part.endswith("bind")]
    credentials = [mount for mount in mounts if mount[1] != "/" and Path(mount[1]).is_file()]
    assert credentials == [("--ro-bind", str(login), str(placeholder))]
    assert ("--setenv", "XDG_DATA_HOME", str(data_home)) in zip(
        command, command[1:], command[2:], strict=False
    )
    assert ("--setenv", "HOME", str(owner_home)) not in zip(
        command, command[1:], command[2:], strict=False
    )

    before = login.stat()
    ran = subprocess.run(
        [
            *command,
            "-c",
            'cat "$XDG_DATA_HOME/opencode/auth.json"; '
            'echo x >> "$XDG_DATA_HOME/opencode/auth.json" && echo WROTE; '
            'mv "$XDG_DATA_HOME/opencode/auth.json" "$XDG_DATA_HOME/moved" && echo MOVED; true',
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert '"made-up"' in ran.stdout, ran.stderr
    assert "WROTE" not in ran.stdout and "MOVED" not in ran.stdout, ran.stdout
    after = login.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    assert placeholder.stat().st_size == 0


@_NEEDS_BWRAP
def test_opencode_refuses_the_owner_data_home(owner_home, tmp_path) -> None:
    from native_isolation import opencode_command

    owner_data = owner_home / ".local" / "share"
    for root in (owner_home, tmp_path / "drill"):
        with pytest.raises(RuntimeError, match="BLOCKED"):
            opencode_command(Path("/bin/sh"), root, owner_data)
    assert not (owner_home / "native-home").exists(), "a refusal made folders in the owner's home"
    assert not (tmp_path / "drill").exists(), "a refusal made the fixture root"
    assert sorted(path.name for path in (owner_data / "opencode").iterdir()) == ["auth.json"]
    assert not (owner_data / "opencode" / "auth.json").is_symlink()
    assert (owner_data / "opencode" / "auth.json").read_text() == '{"made-up": "login"}\n'
