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
from collections.abc import Callable, Iterable
from functools import cache
from pathlib import Path

_OWNED_HOMES: dict[Path, tuple[int, int]] = {}

#: Who made a home: `<pid> <start ticks> <home's minimum start ticks> <boot id>`. Written once by
#: `short_codex_home()` and read by `reap_abandoned_homes()` in a later session.
_OWNER_FILE = ".owner"

#: A home with no readable owner is a legacy one, or one caught between `mkdtemp` and its owner
#: file. Only age tells those apart, and a running Codex writes into its home all the time.
_UNCLAIMED_AGE = 24 * 60 * 60


def _boot_ticks() -> int:
    return int(float(Path("/proc/uptime").read_text().split()[0]) * os.sysconf("SC_CLK_TCK"))


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def short_codex_home() -> Path:
    if not Path("/proc/uptime").is_file():
        raise RuntimeError("BLOCKED: verified Codex fixture cleanup requires Linux procfs")
    _pidfd_functions()
    minimum_start = _boot_ticks() - 1
    home = Path(tempfile.mkdtemp(prefix="ra-cx-", dir="/tmp"))
    home.chmod(0o700)
    descriptor = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    _OWNED_HOMES[home] = (descriptor, minimum_start)
    try:
        owner = os.open(
            _OWNER_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=descriptor,
        )
        with os.fdopen(owner, "w") as written:
            started = _parent_and_start(os.getpid())[1]
            written.write(f"{os.getpid()} {started} {minimum_start} {_boot_id()}\n")
    except BaseException:
        # Nothing has run from it yet, so nothing can attest it: the caller never got a home.
        shutil.rmtree(home, ignore_errors=True)
        os.close(_OWNED_HOMES.pop(home)[0])
        raise
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


def _parent_and_start(pid: int) -> tuple[int, int]:
    fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[1]), int(fields[19])


def _cgroup(pid: int) -> str:
    return (Path("/proc") / str(pid) / "cgroup").read_text()


def _started_by_an_older_app(home: Path, pid: int) -> bool:
    """Whether an unreadable process descends from a running app that predates this home.

    The owner's browser opens a sandboxed renderer mid-drill: same uid, newer than the home, and an
    environment nobody can read. A fixture's processes hang off this test run, a helper it started,
    or init or the user's systemd once detached, and none of those says anything. A helper that
    detached keeps this run's cgroup, and the desktop gives each app its own, so an ancestor in
    this run's cgroup is no app. Compare the ancestor, not the process: tmux moves its panes to a
    scope of their own. Every other doubt stays a refusal, including a parent that vanished during
    the walk. Measured on one desktop: bwrap, gnome-shell, systemd, and Cursor, Claude Desktop and
    Element each in their own scope. Elsewhere an app under systemd itself refuses, and so does one
    started from this run's terminal, which fails a run and never deletes a home.
    """
    try:
        ancestor = _parent_and_start(pid)[0]
        while ancestor not in (0, 1, os.getpid()):
            parent, started = _parent_and_start(ancestor)
            if started < _OWNED_HOMES[home][1]:
                return parent not in (0, 1, os.getpid()) and _cgroup(ancestor) != _cgroup(
                    os.getpid()
                )
            ancestor = parent
    except (OSError, ValueError, IndexError):
        return False
    return False


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
        if _started_by_an_older_app(home, pid):
            return False
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            command = "(gone)"
        raise RuntimeError(
            f"process ownership could not be read; fixture home retained: pid {pid} {command}"
        ) from error


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


def managed_daemons(home: Path) -> set[int]:
    """The pids of this home's Codex daemons, read from outside any pane.

    A process forked from a daemon holds its arguments until it execs, so a parent that is itself
    a daemon means a fork in passing, not a second daemon. It reads every process's arguments first,
    which anyone may read, because it runs on every poll of a drill: a process that is no daemon
    must not stop one, as an unreadable environment would stop the ownership scan. A daemon whose
    environment cannot be read is not counted, so a count of one says what was attested.
    """
    home = _private_home(home)
    found: dict[int, int] = {}
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            if b"--managed-daemon" not in (entry / "cmdline").read_bytes():
                continue
            pid = int(entry.name)
            if _belongs_to(home, pid):
                found[pid] = _parent_and_start(pid)[0]
        except (OSError, ValueError, IndexError, RuntimeError):
            continue
    return {pid for pid, parent in found.items() if parent not in found}


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
            try:
                owned = _belongs_to(home, pid)
            except RuntimeError as refusal:
                # Scanned as owned, unreadable now. Not attested, so no signal, and the scan
                # that follows retries it and still refuses one that stays unreadable.
                if not isinstance(refusal.__cause__, PermissionError):
                    raise
                continue
            if owned and sent(descriptor, signum, None, 0) < 0:
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


def _recorded_owner(descriptor: int) -> tuple[int, int, int, str] | None:
    """The owner a home's `.owner` names, or None when it is absent, not ours or not readable."""
    try:
        opened = os.open(_OWNER_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
    except OSError:
        return None
    try:
        details = os.fstat(opened)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            return None
        pid, started, minimum, boot = os.read(opened, 256).decode().split()
        return int(pid), int(started), int(minimum), boot
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    finally:
        os.close(opened)


def _alive(pid: int, started: int) -> bool:
    try:
        return _parent_and_start(pid)[1] == started
    except (OSError, ValueError, IndexError):
        return False


def reap_abandoned_homes(candidates: Iterable[Path] | None = None) -> list[Path]:
    """Remove the drill homes whose drill died before its own clean-up; return those removed.

    A drill killed mid-run never reaches `retire_codex_home`, and its Codex daemon outlives it,
    still logged in (BL-133). The test session's controller calls this once at start
    (`tests/conftest.py`). It looks at `/tmp/ra-cx-*`, or only at `candidates`.

    A home is a candidate only as `short_codex_home()` makes one: a real folder, not a link, of
    this uid, mode 0700 exactly, directly in `/tmp`. Then:

    - its `.owner` names a live process (same pid, same start time, same boot): left alone;
    - it names a dead one: the processes attesting the home are stopped by the ownership rule
      above, and then it is removed;
    - it has no readable `.owner`: removed only when untouched for 24 hours with nothing
      attesting it, since a home a few microseconds old has no owner file yet either.

    The owner's start ticks bound the scan, as at retirement, so a process older than the home is
    never read. A home from an earlier boot, or one with no owner file, has no such bound: every
    process of this uid is read, and one whose environment nobody can read keeps the home. On a
    desktop that is the usual case (ssh-agent and sd-pam are such processes), so those two kinds
    are best effort and mostly kept; tmpfiles' `D /tmp` empties `/tmp` at boot on this host
    anyway. Any doubt or error keeps the home, and the next session tries again.

    The owner is looked up in this process's pid namespace. `short_codex_home()` runs in pytest
    on the host, never inside a drill's confinement, so the two always share one.
    """
    if not Path("/proc/uptime").is_file():
        return []
    removed: list[Path] = []
    homes = sorted(Path("/tmp").glob("ra-cx-*")) if candidates is None else list(candidates)
    for home in homes:
        if home in _OWNED_HOMES:
            continue
        try:
            if _reaped(home):
                removed.append(home)
        except (OSError, RuntimeError, ValueError):
            continue
    return removed


def _reaped(home: Path) -> bool:
    """Reap one candidate home by the rules of `reap_abandoned_homes`; whether it was removed."""
    details = home.lstat()
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o700
        or home.parent != Path("/tmp")
        or not home.name.startswith("ra-cx-")
        or home != home.resolve()
    ):
        return False
    descriptor: int | None = os.open(
        home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        opened = os.fstat(descriptor)
        owner = _recorded_owner(descriptor)
        this_boot = owner is not None and owner[3] == _boot_id()
        if (
            (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino)
            or (this_boot and _alive(owner[0], owner[1]))
            or (owner is None and time.time() - details.st_mtime < _UNCLAIMED_AGE)
        ):
            return False
        _OWNED_HOMES[home], descriptor = (descriptor, owner[2] if this_boot else 0), None
        if owner is None and _running_from(home):
            return False
        retire_codex_home(home)
        return True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        claim = _OWNED_HOMES.pop(home, None)
        if claim is not None:
            os.close(claim[0])
