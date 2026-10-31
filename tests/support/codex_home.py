"""Private Codex homes and bounded cleanup of their owned Linux processes.

A short home avoids Codex's Unix-socket path limit. Process ownership is the exact
private CODEX_HOME environment plus uid, independent of executable location and
parent survival. Signals use pidfds and re-check ownership, so a stale PID list
cannot authorize a different process. Unreadable ownership or incomplete reaping
retains the directory and fails instead of claiming successful cleanup.
"""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import signal
import stat
import tempfile
import time
from collections.abc import Callable
from functools import cache
from pathlib import Path

_OWNED_HOMES: dict[Path, tuple[int, int]] = {}


def _boot_ticks() -> int:
    return int(float(Path("/proc/uptime").read_text().split()[0]) * os.sysconf("SC_CLK_TCK"))


def short_codex_home() -> Path:
    if not Path("/proc/uptime").is_file():
        raise RuntimeError("BLOCKED: verified Codex fixture cleanup requires Linux procfs")
    _pidfd_functions()
    minimum_start = _boot_ticks() - 1
    home = Path(tempfile.mkdtemp(prefix="ra-cx-", dir="/tmp"))
    home.chmod(0o700)
    descriptor = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    _OWNED_HOMES[home] = (descriptor, minimum_start)
    return home


def _private_home(home: Path) -> Path:
    details = home.lstat()
    ownership = _OWNED_HOMES.get(home)
    original = None if ownership is None else os.fstat(ownership[0])
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
        or home != home.resolve()
        or not home.name.startswith("ra-cx-")
        or home.parent != Path("/tmp")
        or ownership is None
        or (original.st_dev, original.st_ino) != (details.st_dev, details.st_ino)
    ):
        raise ValueError("cleanup requires the exact owned private Codex fixture home")
    if not Path("/proc").is_dir():
        raise RuntimeError("Codex fixture process ownership is unavailable on this platform")
    return home


def _belongs_to(home: Path, pid: int) -> bool:
    entry = Path("/proc") / str(pid)
    try:
        if entry.stat().st_uid != os.getuid():
            return False
        # Zombies cannot run or retain an authenticated daemon. Reading state also avoids
        # treating their empty environment as a live process whose identity disappeared.
        fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] in {"Z", "X"}:
            return False
        # Field 22 is starttime; the list begins at field 3. A process older than
        # this controller-minted home cannot belong to the fixture, so its private
        # environment is neither needed nor read.
        if int(fields[19]) < _OWNED_HOMES[home][1]:
            return False
        environment = (entry / "environ").read_bytes().split(b"\0")
        return os.fsencode(f"CODEX_HOME={home}") in environment
    except (FileNotFoundError, ProcessLookupError):
        return False
    except PermissionError as error:
        raise RuntimeError("process ownership could not be read; fixture home retained") from error


def _running_from(home: Path) -> list[int]:
    """Live processes attesting this fixture home, including detached installed binaries."""
    home = _private_home(home)
    deadline = time.monotonic() + 2
    while True:
        try:
            return [
                int(entry.name)
                for entry in Path("/proc").glob("[0-9]*")
                if _belongs_to(home, int(entry.name))
            ]
        except RuntimeError as error:
            # An exec/setuid transition may briefly hide an otherwise readable
            # process. Retry the entire scan; never skip an unattested process.
            if not isinstance(error.__cause__, PermissionError) or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


@cache
def _pidfd_functions():
    # python-build-standalone may omit Python's pidfd wrappers even when libc provides
    # them. Resolve the native symbols; never fall back to a racy numeric-PID signal.
    library = ctypes.CDLL(None, use_errno=True)
    try:
        opened = library.pidfd_open
        sent = library.pidfd_send_signal
    except AttributeError as error:
        raise RuntimeError("pidfd cleanup is unavailable; fixture home retained") from error
    opened.argtypes = (ctypes.c_int, ctypes.c_uint)
    opened.restype = ctypes.c_int
    sent.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
    sent.restype = ctypes.c_int
    return opened, sent


def _signal(home: Path, pids: list[int], signum: int) -> None:
    home = _private_home(home)
    opened, sent = _pidfd_functions()
    for pid in pids:
        descriptor = opened(pid, 0)
        if descriptor < 0:
            error = ctypes.get_errno()
            if error == errno.ESRCH:
                continue
            raise OSError(error, os.strerror(error))
        try:
            if _belongs_to(home, pid) and sent(descriptor, signum, None, 0) < 0:
                error = ctypes.get_errno()
                if error != errno.ESRCH:
                    raise OSError(error, os.strerror(error))
        finally:
            os.close(descriptor)


def stop_codex_home(home: Path, *, stop: Callable[[], None] | None = None) -> None:
    """Reap exactly owned processes while retaining the home for a restart drill."""
    home = _private_home(home)
    if stop is not None:
        stop()
    # Never invoke an unconstrained native manager: even a private CODEX_HOME
    # can use a hard-coded host-wide runtime lock. Default cleanup relies only
    # on the attested home and pidfd ownership checks below.
    for signum in (signal.SIGTERM, signal.SIGKILL):
        _signal(home, _running_from(home), signum)
        deadline = time.monotonic() + 3
        while _running_from(home) and time.monotonic() < deadline:
            time.sleep(0.1)
    if _running_from(home):
        raise RuntimeError("owned Codex processes remain; fixture home retained")


def retire_codex_home(home: Path, *, stop: Callable[[], None] | None = None) -> None:
    """Remove only after all processes attesting the fixture home have exited."""
    home = _private_home(home)
    stop_codex_home(home, stop=stop)
    shutil.rmtree(home)
    os.close(_OWNED_HOMES.pop(home)[0])
