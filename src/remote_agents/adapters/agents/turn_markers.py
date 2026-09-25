"""The per-session "a turn started" markers, as small files in the activity spool (DEC-104).

Written by the agent hook (`activity_spool`), read and ended by the service under a pane's key
lock. A marker is a file named after the session under `<activity directory>/turns/`, and its
modification time is when the turn started. It holds at most one thing: the id the starting
agent gave its own session, from its hook payload -- an identifier, never anything typed. That
owner is what keeps an agent started inside a managed pane, which inherits the pane's session
id, from ending its parent's turn with its own `Stop`: a hook's end removes the marker only
when it comes from the owner, or when no owner is recorded. The service's end, on a screen that
shows the turn over, is unconditional. The drain globs `*.json` at the top of the
spool, so this subdirectory is never read as a record.

The hook side inherits the spool's rules, for the spool's reasons (see `activity_spool`'s
docstring): the name comes from the environment, so it is accepted only as a session id; the
directory is opened through `open_private_directory`, which refuses a link left where it
belongs; a marker is opened with `O_NOFOLLOW`, so a link planted at its name is refused rather
than written through; and nothing here raises -- every `OSError` answers "no marker". A lost
marker costs the relay the protection it adds and nothing more: without one it reads the screen
alone, which is its behaviour before this existed.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from remote_agents.ports.private_directory import open_private_directory
from remote_agents.ports.session_identity import safe_session_id

TURNS_DIRECTORY = "turns"

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_CREATE = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | _CLOEXEC
_READ = os.O_RDONLY | os.O_NOFOLLOW | _CLOEXEC
#: One byte past the longest id `safe_session_id` accepts, so an over-long file reads as no owner.
_OWNER_BYTES = 129


class FileTurnMarkers:
    """`TurnMarkers` as one 0600 file per session in a private directory, holding at most the
    id its starting agent gave itself."""

    def __init__(self, activity_directory: Path) -> None:
        self._directory = activity_directory / TURNS_DIRECTORY

    def start(self, session_id: str, owner: str | None = None) -> None:
        name = safe_session_id(session_id)
        if name is None:
            return
        directory = open_private_directory(self._directory)
        if directory is None:
            return
        recorded = safe_session_id(owner)
        try:
            descriptor = os.open(directory / name, _CREATE, 0o600)
        except OSError:
            return
        try:
            # A marker another agent owns keeps its owner: that is a nested agent's submit,
            # landing on its parent's running turn.
            if _owner_of(descriptor) in (None, recorded):
                os.ftruncate(descriptor, 0)
                os.pwrite(descriptor, (recorded or "").encode("ascii"), 0)
            # Refreshed, not merely created: the relay trusts a young marker over the screen,
            # and "young" is measured from the latest submit, not from the first one.
            os.utime(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def end(self, session_id: str, owner: str | None = None) -> None:
        name = safe_session_id(session_id)
        if name is None or not self._is_real_directory():
            return
        if owner is not None:
            # A hook's end: only the agent that started the marker ends it. A marker with no
            # owner recorded has none to protect, and ends as before owners existed.
            try:
                descriptor = os.open(self._directory / name, _READ)
            except OSError:
                return
            try:
                current = _owner_of(descriptor)
            except OSError:
                current = None
            finally:
                os.close(descriptor)
            if current is not None and current != safe_session_id(owner):
                return
        try:
            # `unlink` removes a link rather than what it points at, so a planted one is only
            # ever deleted, never followed.
            os.unlink(self._directory / name)
        except OSError:
            return

    def started_at(self, session_id: str) -> datetime | None:
        name = safe_session_id(session_id)
        if name is None or not self._is_real_directory():
            return None
        try:
            status = os.lstat(self._directory / name)
        except OSError:
            return None
        if not stat.S_ISREG(status.st_mode):
            return None
        return datetime.fromtimestamp(status.st_mtime, UTC)

    def sessions(self) -> tuple[str, ...]:
        if not self._is_real_directory():
            return ()
        try:
            entries = list(os.scandir(self._directory))
        except OSError:
            return ()
        return tuple(
            entry.name
            for entry in entries
            if safe_session_id(entry.name) is not None and _is_regular(entry)
        )

    def _is_real_directory(self) -> bool:
        try:
            return stat.S_ISDIR(os.lstat(self._directory).st_mode)
        except OSError:
            return False


def _owner_of(descriptor: int) -> str | None:
    """The agent id a marker holds, or None when it holds none that reads as one."""
    try:
        text = os.pread(descriptor, _OWNER_BYTES, 0).decode("ascii")
    except UnicodeDecodeError:
        return None
    return safe_session_id(text)


def _is_regular(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False
