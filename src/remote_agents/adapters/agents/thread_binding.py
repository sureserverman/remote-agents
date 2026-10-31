"""One launch's nonce, spent once by the shell the service typed it into, binds a thread.

A fresh conversation has no resume id, and nothing in a hook payload ties a thread to the pane that
opened it. So the service mints a nonce for each launch, keeps only its hash, and types one command
carrying it into its own pane. That command's shell knows its thread, so spending the nonce records
which thread is the session's:

    <activity directory>/thread-binding/pending/<session>   the nonce's hash, until it is spent
    <activity directory>/thread-binding/threads/<thread>    the session that thread belongs to

This guards against misattribution, not a hostile agent. An agent running as the same user can read
this directory and its own pane. What it rules out is a thread being filed under a session because
of a shared folder, a shared daemon or an inherited variable (DEC-128).

Nothing here names a provider (DEC-122). Ids hold no secrets and folders never appear here
(DEC-013 clause 2). Stdlib only, for the same reason as `hook_identity`.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import tempfile
import time
from collections.abc import Collection
from pathlib import Path

from remote_agents.ports.private_directory import open_private_directory
from remote_agents.ports.session_identity import safe_session_id

BINDING_DIRECTORY = "thread-binding"
_PENDING = "pending"
_THREADS = "threads"

#: Work files start with this, and no id does (`safe_session_id`), so they never read as records.
_WORK_PREFIX = "."
_CLAIMED_PREFIX = ".claimed-"
#: A claim lives for one call. One this old belongs to a process that died.
_ABANDONED_SECONDS = 60.0
_MAXIMUM_FILE_BYTES = 256


class UntrustedBinding(Exception):
    """The binding files are there and cannot be trusted. Callers refuse, never guess."""


def mint_nonce(activity_directory: Path, session_id: str) -> str:
    """A fresh nonce for `session_id`, replacing any earlier one. Only its hash is kept.

    Hexadecimal, never URL-safe base64: that alphabet can begin with `-`, which reads as a flag
    on the command line the nonce is typed into.
    """
    if safe_session_id(session_id) is None:
        raise ValueError("not a session id")
    pending = _directory(activity_directory, _PENDING, create=True)
    if pending is None:
        raise UntrustedBinding("cannot make the binding directory")
    # A new launch's conversation is not the old one, even before it binds.
    threads = _directory(activity_directory, _THREADS, create=False)
    if threads is not None:
        _forget_threads_of(threads, session_id)
    nonce = secrets.token_hex(32)
    temporary = _written(pending, _hash(nonce))
    try:
        os.replace(temporary, pending / session_id)
    except BaseException:
        _unlink(temporary)
        raise
    return nonce


def consume_nonce(
    activity_directory: Path, session_id: object, nonce: object, thread_id: object
) -> str | None:
    """Bind `thread_id` to `session_id`. None on success, otherwise why not, naming no secret.

    The pending file is claimed by rename before the thread is written, so of two shells holding
    one nonce exactly one proceeds. A wrong nonce spends nothing. A crash after the claim loses
    the nonce and binds nothing, which the service sees as a binding that never came.
    """
    session = safe_session_id(session_id)
    thread = safe_session_id(thread_id)
    if session is None:
        return "not a session id"
    if thread is None:
        return "this shell's thread is not an id"
    if not isinstance(nonce, str) or not nonce:
        return "no nonce given"
    try:
        pending = _directory(activity_directory, _PENDING, create=False)
        if pending is None:
            return "no nonce is waiting for this session"
        recorded = _read_private(pending / session)
        if recorded is None:
            return "no nonce is waiting for this session"
        if not hmac.compare_digest(recorded, _hash(nonce)):
            return "the nonce does not match"
        threads = _directory(activity_directory, _THREADS, create=True)
        if threads is None:
            return "the binding directory cannot be trusted"
        claimed = pending / f"{_CLAIMED_PREFIX}{session}-{secrets.token_hex(8)}"
        try:
            os.rename(pending / session, claimed)
        except FileNotFoundError:
            return "no nonce is waiting for this session"
        # A new nonce can replace the old between the comparison and the claim. Compare what was
        # claimed, and give back what this shell had no right to take.
        if not hmac.compare_digest(_read_private(claimed) or "", _hash(nonce)):
            _give_back(claimed, pending / session)
            return "the nonce does not match"
        _unlink(claimed)
        why = _link_new(threads, thread, session)
        if why is None:
            _forget_threads_of(threads, session, keep=thread)
        return why
    except (OSError, UntrustedBinding):
        return "the binding directory cannot be trusted"


def bound_session(activity_directory: Path, thread_id: object) -> str | None:
    """The session `thread_id` is bound to, None when it has none. Creates nothing.

    Raises `UntrustedBinding` for a file or directory that is there and cannot be trusted.
    """
    thread = safe_session_id(thread_id)
    if thread is None:
        return None
    threads = _directory(activity_directory, _THREADS, create=False)
    if threads is None:
        return None
    try:
        recorded = _read_private(threads / thread)
    except OSError as error:
        raise UntrustedBinding(str(error)) from error
    if recorded is None:
        return None
    session = safe_session_id(recorded)
    if session is None:
        raise UntrustedBinding("a thread file names no session")
    return session


def prune(activity_directory: Path, live_sessions: Collection[str]) -> None:
    """Forget what belongs to sessions off the roster. Links are removed, never followed.

    A thread bound to an ended session must not outlive it: a later session may resume that thread
    by its resume id. An untrusted directory is left alone, for the refusals to report.
    """
    try:
        for name, belongs in ((_PENDING, _pending_is_live), (_THREADS, _thread_is_live)):
            directory = _directory(activity_directory, name, create=False)
            if directory is None:
                continue
            with os.scandir(directory) as entries:
                for entry in list(entries):
                    if entry.name.startswith(_WORK_PREFIX):
                        if time.time() - entry.stat(follow_symlinks=False).st_mtime > (
                            _ABANDONED_SECONDS
                        ):
                            _unlink(Path(entry.path))
                    elif not belongs(entry, live_sessions):
                        _unlink(Path(entry.path))
    except (OSError, UntrustedBinding):
        return


def _pending_is_live(entry: os.DirEntry[str], live_sessions: Collection[str]) -> bool:
    return entry.name in live_sessions


def _thread_is_live(entry: os.DirEntry[str], live_sessions: Collection[str]) -> bool:
    try:
        return _read_private(Path(entry.path)) in live_sessions
    except (OSError, UntrustedBinding):
        return False


def _hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8", "surrogateescape")).hexdigest()


def _directory(activity_directory: Path, name: str, *, create: bool) -> Path | None:
    """`thread-binding/<name>`, with every level checked: None when absent and not to be made.

    All of them, not only the one asked for: one opened-up level makes the whole store a refusal.
    `open_private_directory` tightens a leaf it finds, so checking first is what keeps that a
    refusal instead of a quiet repair.
    """
    root = activity_directory / BINDING_DIRECTORY
    leaf = root / name
    missing = False
    for directory in (root, root / _PENDING, root / _THREADS):
        try:
            status = os.lstat(directory)
        except FileNotFoundError:
            missing = missing or directory in (root, leaf)
            continue
        except OSError as error:
            raise UntrustedBinding("the binding directory cannot be read") from error
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_mode & 0o077
            or status.st_uid != os.geteuid()
        ):
            raise UntrustedBinding("the binding directory is not private")
    if not missing:
        return leaf
    if not create:
        return None
    if open_private_directory(leaf) is None:
        raise UntrustedBinding("cannot make the binding directory")
    return leaf


def _read_private(path: Path) -> str | None:
    """The file's text, None when absent. Raises `UntrustedBinding` for anything not private."""
    try:
        # Non-blocking, because opening a pipe for reading waits for a writer.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise UntrustedBinding("a binding file cannot be opened") from error
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_mode & 0o077
            or status.st_uid != os.geteuid()
        ):
            raise UntrustedBinding("a binding file is not private")
        raw = os.read(descriptor, _MAXIMUM_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _MAXIMUM_FILE_BYTES:
        raise UntrustedBinding("a binding file is too long")
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise UntrustedBinding("a binding file is not text") from error


def _written(directory: Path, content: str) -> Path:
    """A durable owner-only work file in `directory`. `mkstemp` makes it 0600."""
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f"{_WORK_PREFIX}tmp-")
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        _unlink(Path(temporary))
        raise
    return Path(temporary)


def _link_new(threads: Path, thread: str, session: str) -> str | None:
    """Publish the whole file under the thread's name, or refuse if the name is taken."""
    temporary = _written(threads, session)
    try:
        os.link(temporary, threads / thread)
    except FileExistsError:
        return "this thread is already bound"
    finally:
        _unlink(temporary)
    return None


def _give_back(claimed: Path, pending: Path) -> None:
    """Restore a claimed file, unless a still newer nonce has taken its place."""
    try:
        os.link(claimed, pending)
    except OSError:
        pass
    _unlink(claimed)


def _forget_threads_of(threads: Path, session: str, keep: str | None = None) -> None:
    """A session has one bound thread: its newest. An earlier launch's must stop naming it."""
    with os.scandir(threads) as entries:
        for entry in list(entries):
            if entry.name == keep or entry.name.startswith(_WORK_PREFIX):
                continue
            try:
                if _read_private(Path(entry.path)) == session:
                    _unlink(Path(entry.path))
            except (OSError, UntrustedBinding):
                continue


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
