"""Does stopping a launchd service take its managed tmux sessions with it?

The macOS half of `docs/drill-service-restart-session-survival.md`, which asks the same
question of systemd and answers it with `KillMode=process`. launchd has no such directive: the
analogue is `AbandonProcessGroup`, and it is a *consequence* of what `bootout` signals rather
than a setting whose name says what it does. `tests/contract/supervisor/test_launchd_supervisor.py`
pins that the key is written into the plist. Nothing until now has confirmed launchd behaves
that way on a real Mac.

That is the gap this closes, and it is the whole reason Stage 3 exists rather than ending the
plan at two green adapters. If `AbandonProcessGroup` were wrong, or a later edit dropped it,
stopping the service would silently take every in-flight agent session and the work inside it —
the same loss the Linux drill exists to rule out, on the platform where it has never been
checked.

**Disposable proof (ARCH-11).** Everything here is transient: a label that exists only for this
test, a plist written and deleted, a tmux server of its own. The drill host is a real machine
with real LaunchAgents in `~/Library/LaunchAgents`; none of them is touched, and the teardown
runs whether or not the assertions pass. A drill that leaves anything behind has failed even
when it reports success.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="BLOCKED: the launchd drill can only run on macOS",
)

#: Deliberately not the production label. `bootout` on the wrong one would stop the owner's
#: real service, and this file runs on a machine where that service is meant to be running.
TEST_LABEL = "remote-agents-test-launchd-drill"

#: Matches `TEST_SOCKET_PREFIX` in `ports/tmux_server.py`, so the gateway would refuse to
#: mistake this server for the production one.
PROBE_SOCKET = "remote-agents-test-service"
PROBE_SESSION = "ra-service-probe"

_REPOSITORY = Path(__file__).resolve().parents[2]


def _launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *arguments], capture_output=True, text=True, check=False, timeout=30
    )


def _tmux(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", PROBE_SOCKET, *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _probe_session_exists() -> bool:
    """Exit status only, the same discipline the supervisor port imposes on liveness."""
    return _tmux("has-session", "-t", f"={PROBE_SESSION}").returncode == 0


def _wait_for(predicate, *, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return False


def _homebrew_bin() -> Path | None:
    for candidate in (Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
        if (candidate / "tmux").is_file():
            return candidate
    return None


@pytest.fixture
def transient_agent() -> Iterator[Path]:
    """Write, and unconditionally remove, one throwaway LaunchAgent.

    The teardown is a fixture rather than the end of the test body so that a failed assertion
    still boots the job out and deletes the plist. A drill that leaves a registered agent on the
    owner's machine has done harm the finding would not justify.
    """
    homebrew = _homebrew_bin()
    if homebrew is None:
        pytest.skip("BLOCKED: tmux is not installed; the survival property cannot be observed")

    interpreter = Path(sys.executable)
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{TEST_LABEL}.plist"
    log_directory = Path(tempfile.mkdtemp(prefix="ra-drill-"))
    definition = {
        "Label": TEST_LABEL,
        "ProgramArguments": [str(interpreter), "-m", "remote_agents.service_probe"],
        "WorkingDirectory": str(_REPOSITORY),
        "EnvironmentVariables": {
            # The reason the adapter computes a PATH at all: launchd hands a job
            # `_PATH_STDPATH`, which contains no Homebrew prefix, and `service_probe` invokes
            # `tmux` by bare name. Without this the probe fails to start for a reason that has
            # nothing to do with the property under test.
            "PATH": f"{homebrew}:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONPATH": str(_REPOSITORY / "src"),
        },
        "RunAtLoad": False,
        # The drill needs these for the same reason the production adapter does, and the first
        # run proved it: the probe failed to start, and without them there was nothing to read
        # -- launchd sends a job's output to /dev/null by default. A drill that cannot say *why*
        # it failed is a drill that can only report that something did.
        "StandardOutPath": str(log_directory / "probe.out"),
        "StandardErrorPath": str(log_directory / "probe.err"),
        # The property under test. Without it launchd kills whatever shares the job's process
        # group when the job dies, which would take the probe's tmux server with it.
        "AbandonProcessGroup": True,
        "ExitTimeOut": 15,
        "Umask": 0o077,
    }
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plistlib.dumps(definition, fmt=plistlib.FMT_XML))
    try:
        yield plist_path
    finally:
        _launchctl("bootout", f"gui/{os.getuid()}/{TEST_LABEL}")
        _tmux("kill-server")
        plist_path.unlink(missing_ok=True)
        for log in sorted(log_directory.glob("probe.*")):
            text = log.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                print(f"\n--- {log.name} ---\n{text}", file=sys.stderr)
        shutil.rmtree(log_directory, ignore_errors=True)


def test_a_managed_tmux_session_survives_the_supervisor_stopping_the_service(
    transient_agent: Path,
) -> None:
    """bootstrap -> kickstart -> the session exists -> bootout -> the session is still there.

    The last step is the whole drill. Everything before it establishes that the service really
    was running and really did own the session, so that the survival is evidence rather than a
    coincidence of the session never having been owned in the first place.
    """
    uid = os.getuid()
    target = f"gui/{uid}/{TEST_LABEL}"

    bootstrapped = _launchctl("bootstrap", f"gui/{uid}", str(transient_agent))
    assert bootstrapped.returncode == 0, f"{bootstrapped.stdout}\n{bootstrapped.stderr}"

    started = _launchctl("kickstart", target)
    assert started.returncode == 0, f"{started.stdout}\n{started.stderr}"

    if not _wait_for(_probe_session_exists):
        # The job's own account of itself, printed before the assertion so a failure explains
        # itself instead of only announcing itself.
        print(f"\n--- launchctl print {target} ---", file=sys.stderr)
        print(_launchctl("print", target).stdout[:3000], file=sys.stderr)
        raise AssertionError(
            "the probe never created its tmux session; the drill proves nothing about "
            "survival because there was nothing to survive"
        )

    booted_out = _launchctl("bootout", target)
    assert booted_out.returncode == 0, f"{booted_out.stdout}\n{booted_out.stderr}"

    # The service is gone -- asserted, so that a session outliving a job that never stopped
    # cannot be mistaken for a session outliving one that did.
    assert _wait_for(lambda: _launchctl("print", target).returncode != 0), (
        "the service was still registered after bootout"
    )

    assert _probe_session_exists(), (
        "the managed tmux session died with the service. On Linux this is what "
        "KillMode=process prevents; on macOS AbandonProcessGroup is the analogue, and this "
        "is the failure it exists to rule out -- a service restart would take every "
        "in-flight agent session and the work inside it."
    )


def test_the_drill_leaves_nothing_registered(transient_agent: Path) -> None:
    """ARCH-11: the proof is disposable, and that is itself worth asserting.

    Runs the teardown's own conditions from inside a second test so that "nothing was left
    behind" is a checked claim rather than an intention stated in a docstring.
    """
    uid = os.getuid()
    _launchctl("bootstrap", f"gui/{uid}", str(transient_agent))
    _launchctl("bootout", f"gui/{uid}/{TEST_LABEL}")

    assert _launchctl("print", f"gui/{uid}/{TEST_LABEL}").returncode != 0
    assert TEST_LABEL not in _launchctl("list").stdout
