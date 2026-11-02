"""Fixture cleanup owns processes by their private home, never executable location."""

import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from codex_home import _running_from, retire_codex_home, short_codex_home, stop_codex_home

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


def test_signal_leaves_a_process_it_cannot_read_to_the_next_scan(monkeypatch):
    """Scanned as owned, then unreadable: no signal and no verdict here. The scan decides."""
    import codex_home

    home = short_codex_home()
    process, pid = child(home)

    def belongs(*_):
        try:
            raise PermissionError("unreadable since the scan")
        except PermissionError as error:
            raise RuntimeError("process ownership could not be read") from error

    with monkeypatch.context() as patch:
        patch.setattr(codex_home, "_belongs_to", belongs)
        try:
            codex_home._signal(home, [pid], signal.SIGTERM)
            assert process.poll() is None, "an unattested process must not be signalled"
        finally:
            patch.undo()
            stop_child(process, pid)
            shutil.rmtree(home)


def test_signal_still_raises_what_is_not_unreadability(monkeypatch):
    import codex_home

    home = short_codex_home()
    process, pid = child(home)

    def belongs(*_):
        raise RuntimeError("something else")

    with monkeypatch.context() as patch:
        patch.setattr(codex_home, "_belongs_to", belongs)
        try:
            with pytest.raises(RuntimeError, match="something else"):
                codex_home._signal(home, [pid], signal.SIGTERM)
        finally:
            patch.undo()
            stop_child(process, pid)
            shutil.rmtree(home)


def test_an_ownership_refusal_names_the_process(monkeypatch):
    """The cleanup error came back three runs with its process gone and nothing naming it."""
    import codex_home

    home = short_codex_home()
    real_read_bytes = Path.read_bytes

    def read_bytes(path):
        if path.name == "environ":
            raise PermissionError("unreadable")
        return real_read_bytes(path)

    process = subprocess.Popen(["sleep", "30"])
    try:
        monkeypatch.setattr(Path, "read_bytes", read_bytes)
        monkeypatch.setattr(codex_home, "_started_by_an_older_app", lambda *_: False)
        with pytest.raises(RuntimeError, match=rf"pid {process.pid} .*sleep 30"):
            codex_home._belongs_to(home, process.pid)
    finally:
        monkeypatch.undo()
        process.kill()
        process.wait()
        shutil.rmtree(home)


def test_restart_stop_reaps_only_owned_processes_and_retains_home():
    home, sibling_home = short_codex_home(), short_codex_home()
    owned, pid = child(home)
    sibling, sibling_pid = child(sibling_home)
    marker = home / "conversation-evidence"
    marker.write_text("retained")
    try:
        stop_codex_home(home, stop=lambda: None)
        assert owned.wait(timeout=5) != 0
        assert marker.read_text() == "retained"
        assert _running_from(home) == []
        assert sibling.poll() is None
        assert sibling_pid in _running_from(sibling_home)
    finally:
        stop_child(owned, pid)
        stop_child(sibling, sibling_pid)
        retire_codex_home(home, stop=lambda: None)
        retire_codex_home(sibling_home, stop=lambda: None)


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
                state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
                if state not in {"Z", "X"} and os.readlink(entry / "ns/pid") == value:
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
        deadline = time.monotonic() + 2
        while members(value) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not members(value), "a detached daemon survived its fixture namespace"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        if held is not None:
            os.close(held)
        retire_codex_home(home, stop=lambda: None)


def test_process_scan_retries_transient_unreadability_without_skipping(monkeypatch):
    import codex_home

    home = short_codex_home()
    attempts = []

    def belongs(_home, _pid):
        attempts.append(_pid)
        if len(attempts) == 1:
            try:
                raise PermissionError("exec transition")
            except PermissionError as error:
                raise RuntimeError("process ownership could not be read") from error
        return False

    monkeypatch.setattr(codex_home, "_belongs_to", belongs)
    monkeypatch.setattr(codex_home.time, "sleep", lambda _: None)
    try:
        assert _running_from(home) == []
        assert len(attempts) > 1
    finally:
        shutil.rmtree(home)


def test_process_scan_stays_closed_on_persistent_unreadability(monkeypatch):
    import codex_home

    home = short_codex_home()
    ticks = iter([0, 1, 3])

    def belongs(*_):
        try:
            raise PermissionError("unreadable")
        except PermissionError as error:
            raise RuntimeError("process ownership could not be read") from error

    monkeypatch.setattr(codex_home, "_belongs_to", belongs)
    monkeypatch.setattr(codex_home.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(codex_home.time, "sleep", lambda _: None)
    try:
        with pytest.raises(RuntimeError, match="ownership could not be read"):
            _running_from(home)
        assert home.exists()
    finally:
        shutil.rmtree(home)


def test_default_retirement_never_runs_an_unconfined_native_cli(monkeypatch):
    home = short_codex_home()
    process, pid = child(home)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unconfined daemon management attempted")

    monkeypatch.setattr(subprocess, "run", forbidden)
    try:
        retire_codex_home(home)
        assert process.wait(timeout=5) != 0
        assert not home.exists()
    finally:
        stop_child(process, pid)
        shutil.rmtree(home, ignore_errors=True)


_UNREADABLE = (
    "import ctypes, time; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0); print('ready', flush=True); "
    "time.sleep(30)"
)
#: Waits for a line, then starts one process whose environment this user cannot read.
_LAUNCHER = (
    "import subprocess, sys; sys.stdin.readline(); "
    f"child = subprocess.Popen([sys.executable, '-c', {_UNREADABLE!r}], stdout=subprocess.PIPE); "
    "print(child.pid, flush=True); child.stdout.readline(); print('up', flush=True); "
    "sys.stdin.readline(); child.kill(); child.wait()"
)


def _unreadable(pid: int) -> bool:
    try:
        Path(f"/proc/{pid}/environ").read_bytes()
    except PermissionError:
        return True
    return False


def test_an_unreadable_process_from_an_older_app_is_not_the_fixtures() -> None:
    """The owner's browser opened a renderer mid-drill, and cleanup refused: same uid, newer than
    the home, and an environment nobody can read (2026-11-01). Its parents reach an app older than
    the home that is not this test run, and a fixture's process has no such parent."""
    import codex_home

    # Under a shell, so the app older than the home is not this run's own child.
    launcher = subprocess.Popen(
        ["bash", "-c", f"{shlex.quote(sys.executable)} -c {shlex.quote(_LAUNCHER)}; :"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.2)
    home = short_codex_home()
    try:
        launcher.stdin.write("go\n")
        launcher.stdin.flush()
        child = int(launcher.stdout.readline())
        assert launcher.stdout.readline().strip() == "up"
        assert _unreadable(child)
        assert codex_home._belongs_to(home, child) is False
    finally:
        launcher.stdin.write("stop\n")
        launcher.stdin.close()
        launcher.wait(10)
        retire_codex_home(home)


def test_an_unreadable_child_of_this_run_still_refuses_cleanup() -> None:
    import codex_home

    home = short_codex_home()
    child = subprocess.Popen([sys.executable, "-c", _UNREADABLE], stdout=subprocess.PIPE)
    try:
        assert child.stdout.readline().strip() == b"ready"
        assert _unreadable(child.pid)
        with pytest.raises(RuntimeError, match="ownership could not be read"):
            codex_home._belongs_to(home, child.pid)
    finally:
        child.kill()
        child.wait()
        shutil.rmtree(home)
        os.close(codex_home._OWNED_HOMES.pop(home)[0])


def test_an_unreadable_orphan_still_refuses_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A daemon that detached hangs off init or the user's systemd, so those decide nothing."""
    import codex_home

    home = short_codex_home()
    chain = {50: (40, 10**12), 40: (1, 5), 1: (0, 1)}
    monkeypatch.setattr(codex_home, "_parent_and_start", lambda pid: chain[pid])
    try:
        assert codex_home._started_by_an_older_app(home, 50) is False
    finally:
        shutil.rmtree(home)
        os.close(codex_home._OWNED_HOMES.pop(home)[0])


def test_a_helper_of_this_run_older_than_the_home_still_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tmux server or helper the run started first can still hold a fixture process."""
    import codex_home

    home = short_codex_home()
    chain = {50: (40, 10**12), 40: (os.getpid(), 5)}
    monkeypatch.setattr(codex_home, "_parent_and_start", lambda pid: chain[pid])
    try:
        assert codex_home._started_by_an_older_app(home, 50) is False
    finally:
        shutil.rmtree(home)
        os.close(codex_home._OWNED_HOMES.pop(home)[0])
