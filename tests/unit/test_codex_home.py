"""Fixture cleanup owns processes by their private home, never executable location."""

import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from codex_home import _running_from, retire_codex_home, short_codex_home

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not Path("/proc").is_dir(),
    reason="Linux process ownership controls are required",
)


@pytest.fixture(autouse=True)
def release_removed_home_handles():
    import codex_home

    before = set(codex_home._OWNED_HOMES)
    yield
    for path in set(codex_home._OWNED_HOMES) - before:
        if not path.exists():
            os.close(codex_home._OWNED_HOMES.pop(path)[0])


def child(home, *, detached=False):
    code = (
        "import os,time; "
        + ("pid=os.fork(); os._exit(0) if pid else os.setsid(); " if detached else "")
        + "print(os.getpid(),flush=True); time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        env={**os.environ, "CODEX_HOME": str(home)},
        stdout=subprocess.PIPE,
        text=True,
    )
    assert select.select([process.stdout], [], [], 5)[0], "owned child did not start"
    pid = int(process.stdout.readline())
    process.stdout.close()
    if detached:
        assert process.wait(timeout=5) == 0
    return process, pid


def stop_child(process, pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


@pytest.mark.parametrize("detached", [False, True])
def test_owned_installed_executable_and_reparented_daemon_are_detected(detached):
    home = short_codex_home()
    process, pid = child(home, detached=detached)
    try:
        assert not Path(os.readlink(f"/proc/{pid}/exe")).is_relative_to(home)
        assert pid in _running_from(home)
    finally:
        stop_child(process, pid)
        shutil.rmtree(home)


def test_retirement_reaps_owned_daemon_and_spares_another_home():
    import codex_home

    home, sibling_home = short_codex_home(), short_codex_home()
    owned, pid = child(home, detached=True)
    sibling, sibling_pid = child(sibling_home)
    stopped = []
    opened, _ = codex_home._pidfd_functions()
    descriptor = opened(pid, 0)
    assert descriptor >= 0
    try:
        retire_codex_home(home, stop=lambda: stopped.append("stopped"))
        assert stopped == ["stopped"]
        assert not home.exists()
        assert select.select([descriptor], [], [], 0)[0], "owned daemon is still alive"
        assert sibling.poll() is None
        assert sibling_pid in _running_from(sibling_home)
    finally:
        os.close(descriptor)
        stop_child(owned, pid)
        stop_child(sibling, sibling_pid)
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(sibling_home)


def test_signal_rechecks_ownership_before_touching_a_stale_pid():
    import codex_home

    home, other = short_codex_home(), short_codex_home()
    process, pid = child(other)
    try:
        codex_home._signal(home, [pid], signal.SIGTERM)
        assert process.poll() is None, "a PID snapshot cannot authorize another home's process"
    finally:
        stop_child(process, pid)
        shutil.rmtree(home)
        shutil.rmtree(other)


def test_failed_reaping_retains_home_and_reports_incomplete_cleanup(monkeypatch):
    import codex_home

    home = short_codex_home()
    process, pid = child(home)
    monkeypatch.setattr(codex_home, "_signal", lambda *_: (_ for _ in ()).throw(OSError("denied")))
    try:
        with pytest.raises(OSError, match="denied"):
            retire_codex_home(home, stop=lambda: None)
        assert home.exists()
        assert process.poll() is None
    finally:
        stop_child(process, pid)
        shutil.rmtree(home)


def test_replaced_home_is_not_authorized_for_cleanup():
    home = short_codex_home()
    original = home.with_name(home.name + "-original")
    home.rename(original)
    home.mkdir(mode=0o700)
    try:
        with pytest.raises(ValueError, match="exact owned private"):
            retire_codex_home(home, stop=lambda: None)
        assert home.is_dir() and original.is_dir()
    finally:
        shutil.rmtree(home)
        shutil.rmtree(original)


def test_native_pid_namespace_ends_a_detached_child_even_if_it_clears_home(tmp_path):
    import runpy

    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("Linux bwrap is unavailable")
    namespace = runpy.run_path(
        str(Path(__file__).parents[1] / "live/test_codex_permission_presets.py")
    )
    home = short_codex_home()
    auth = tmp_path / "dummy-auth"
    auth.write_text("{}")
    (home / "auth.json").touch(mode=0o600)
    identity, stop = tmp_path / "namespace", tmp_path / "stop"
    daemon = (
        "import os,time; from pathlib import Path; "
        f"Path({str(identity)!r}).write_text(os.readlink('/proc/self/ns/pid')); time.sleep(60)"
    )
    code = (
        "import os,sys,time; from pathlib import Path\n"
        f"assert os.readlink('/proc/self/ns/pid') != {os.readlink('/proc/self/ns/pid')!r}\n"
        "if os.fork() == 0:\n"
        " os.setsid(); environment=dict(os.environ); environment.pop('CODEX_HOME',None)\n"
        f" os.execve(sys.executable,[sys.executable,'-c',{daemon!r}],environment)\n"
        "else:\n"
        f" while not Path({str(stop)!r}).exists(): time.sleep(0.01)\n"
    )
    process = subprocess.Popen(
        [*namespace["_prefix"](Path(bwrap), tmp_path, home, auth), sys.executable, "-c", code],
        env={**os.environ, "CODEX_HOME": str(home)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def members(value):
        result = []
        for entry in Path("/proc").glob("[0-9]*"):
            try:
                if os.readlink(entry / "ns/pid") == value:
                    result.append(int(entry.name))
            except OSError:
                continue
        return result

    held = None
    try:
        deadline = time.monotonic() + 5
        while not identity.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert identity.exists(), process.communicate(timeout=5)[1]
        value = identity.read_text()
        observed = members(value)
        assert observed, "fixture namespace was not independently observable"
        # Hold the namespace inode so its identity cannot be recycled during the check.
        held = os.open(f"/proc/{observed[0]}/ns/pid", os.O_RDONLY)
        stop.touch()
        assert process.wait(timeout=5) == 0
        assert not members(value), "a detached daemon survived its fixture namespace"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        if held is not None:
            os.close(held)
        retire_codex_home(home, stop=lambda: None)
