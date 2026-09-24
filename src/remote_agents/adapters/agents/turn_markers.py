"""The per-session "a turn started" markers, as empty files in the activity spool (DEC-104).

Written by the agent hook (`activity_spool`), read and ended by the service under a pane's key
lock. A marker is an empty file named after the session under `<activity directory>/turns/`,
and its modification time is when the turn started. The drain globs `*.json` at the top of the
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

_CREATE = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


class FileTurnMarkers:
    """`TurnMarkers` as one empty 0600 file per session in a private directory."""

    def __init__(self, activity_directory: Path) -> None:
        self._directory = activity_directory / TURNS_DIRECTORY

    def start(self, session_id: str) -> None:
        name = safe_session_id(session_id)
        if name is None:
            return
        directory = open_private_directory(self._directory)
        if directory is None:
            return
        try:
            descriptor = os.open(directory / name, _CREATE, 0o600)
        except OSError:
            return
        try:
            # Refreshed, not merely created: the relay trusts a young marker over the screen,
            # and "young" is measured from the latest submit, not from the first one.
            os.utime(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def end(self, session_id: str) -> None:
        name = safe_session_id(session_id)
        if name is None or not self._is_real_directory():
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


def _is_regular(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False
