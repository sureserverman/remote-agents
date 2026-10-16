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

Beside the markers, `FileConversationMarks` keeps each session's own conversation under
`<activity directory>/conversations/`, written by the hook at a turn's start when its agent
owns the marker, and read by the resume after a restart. Its one known blind spot: an
interrupted turn fires no `Stop`, so if the owner interrupts, `/clear`s and submits before the
service has ended the stale marker on the screen's evidence, the new conversation's agent does
not own the turn and the mark keeps the previous conversation. No hook payload tells that
submit from a nested agent's, which must never win.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from remote_agents.ports.private_directory import open_private_directory
from remote_agents.ports.session_identity import safe_session_id

TURNS_DIRECTORY = "turns"
CONVERSATIONS_DIRECTORY = "conversations"

#: Non-blocking, both: a read-only open of a FIFO planted at a marker's name waits for a writer,
#: and a hook waiting there hangs the owner's live session. On a regular file it changes nothing.
_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
_CREATE = os.O_RDWR | os.O_CREAT | _FLAGS
_READ = os.O_RDONLY | _FLAGS
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

    def end_if_owned_by(self, session_id: str, owner: object) -> None:
        name = safe_session_id(session_id)
        if name is None or not self._is_real_directory():
            return
        # A hook's end: only the agent that started the marker ends it, and an end that names
        # no usable agent ends none that has an owner. A marker with no owner recorded has none
        # to protect, and ends as before owners existed.
        try:
            descriptor = os.open(self._directory / name, _READ)
        except OSError:
            return
        try:
            current = _owner_of(descriptor)
        finally:
            os.close(descriptor)
        if current is None or current == safe_session_id(owner):
            self.end(session_id)

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

    def owner(self, session_id: str) -> str | None:
        name = safe_session_id(session_id)
        if name is None or not self._is_real_directory():
            return None
        try:
            descriptor = os.open(self._directory / name, _READ)
        except OSError:
            return None
        try:
            return _owner_of(descriptor)
        finally:
            os.close(descriptor)

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

    def listable(self) -> bool:
        return _listable(self._directory)

    def _is_real_directory(self) -> bool:
        try:
            return stat.S_ISDIR(os.lstat(self._directory).st_mode)
        except OSError:
            return False


class FileConversationMarks:
    """`ConversationMarks` as one 0600 file per session in a private directory.

    Beside the turn markers, under `<activity directory>/conversations/`, and in this module so
    the hook's path imports nothing more. A mark is written to a fresh temporary and renamed
    into place: a reader never sees a half-written id, which `safe_session_id` would accept as
    a different, shorter one. The rename replaces whatever stood at the name -- a planted link
    included -- and never writes through it. Every read goes through the turn markers' rules:
    `O_NOFOLLOW`, non-blocking, a regular file only, and an id that reads as one or nothing.
    """

    def __init__(self, activity_directory: Path) -> None:
        self._directory = activity_directory / CONVERSATIONS_DIRECTORY

    def record(self, session_id: str, conversation_id: str) -> None:
        name = safe_session_id(session_id)
        conversation = safe_session_id(conversation_id)
        if name is None or conversation is None:
            return
        directory = open_private_directory(self._directory)
        if directory is None:
            return
        temporary = directory / f".pending-{os.urandom(6).hex()}"
        try:
            descriptor = os.open(temporary, _CREATE | os.O_EXCL, 0o600)
        except OSError:
            return
        try:
            try:
                os.write(descriptor, conversation.encode("ascii"))
                # Durable before it is visible: the mark exists for the resume after a power
                # loss, and a rename that outlives its data would leave an empty file. One
                # fsync of a few dozen bytes, on a submit only.
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, directory / name)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def latest(self, session_id: str) -> str | None:
        name = safe_session_id(session_id)
        if name is None or not _is_real_directory(self._directory):
            return None
        try:
            descriptor = os.open(self._directory / name, _READ)
        except OSError:
            return None
        try:
            return _owner_of(descriptor)
        finally:
            os.close(descriptor)

    def sessions(self) -> tuple[str, ...]:
        if not _is_real_directory(self._directory):
            return ()
        try:
            entries = list(os.scandir(self._directory))
        except OSError:
            return ()
        _clear_abandoned(entries)
        return tuple(
            entry.name
            for entry in entries
            if safe_session_id(entry.name) is not None and _is_regular(entry)
        )

    def listable(self) -> bool:
        return _listable(self._directory)

    def forget(self, session_id: str) -> None:
        name = safe_session_id(session_id)
        if name is None or not _is_real_directory(self._directory):
            return
        try:
            os.unlink(self._directory / name)
        except OSError:
            return


#: A `.pending-*` temporary older than this was left by a hook killed between its create and
#: its rename; nothing still writing one is anywhere near this old.
_ABANDONED_SECONDS = 3600


def _clear_abandoned(entries: list[os.DirEntry[str]]) -> None:
    """Collect the temporaries killed hooks left behind; never raises."""
    horizon = datetime.now(UTC).timestamp() - _ABANDONED_SECONDS
    for entry in entries:
        if not entry.name.startswith(".pending-"):
            continue
        try:
            if entry.stat(follow_symlinks=False).st_mtime < horizon:
                os.unlink(entry.path)
        except OSError:
            continue


def _is_real_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _listable(path: Path) -> bool:
    """True when `path` is absent (nothing was ever marked) or a real directory this process
    can list; False for anything else, which a reader would otherwise take for empty."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISDIR(status.st_mode):
        return False
    try:
        with os.scandir(path):
            return True
    except OSError:
        return False


def _owner_of(descriptor: int) -> str | None:
    """The agent id a marker holds, or None when it holds none that reads as one; never raises.

    Only a regular file is read: a FIFO, directory or socket at the name is answered None
    before any read. Both opens are non-blocking, so reaching here never waited on a FIFO --
    measured on Linux and on macOS (Darwin 25.6.0, 2026-09-25).
    """
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        return safe_session_id(os.pread(descriptor, _OWNER_BYTES, 0).decode("ascii"))
    except (OSError, UnicodeDecodeError):
        return None


def _is_regular(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False
