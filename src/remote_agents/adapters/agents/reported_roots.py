"""`ReportedRoots` as one private file per session under `<activity directory>/roots/`.

`remote-agents session-id` writes it when its caller reports, through `PLANNING_HANDOFF_ROOT`,
the git top level it writes handoff envelopes under. The file holds that path and a newline.

The write is `FileConversationMarks.record`'s: the directory through `open_private_directory`,
a fresh `.pending-*` temporary created `O_EXCL` 0600, fsync, then `os.replace`, so a reader never
sees half a path and a link planted at the name is replaced, never written through.

The read refuses what a tampering process could leave: it opens with `O_NOFOLLOW|O_NONBLOCK`, and
needs a regular file this user owns that no one else can read or write, at most 4096 bytes. The
content must pass the same acceptance rule the write applied. That rule lives in the workflow
adapter, which this one may not import, so the composition root hands it in as `accept`.
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

from remote_agents.adapters.agents.turn_markers import (
    _clear_abandoned,
    _is_real_directory,
    _is_regular,
)
from remote_agents.ports.private_directory import open_private_directory
from remote_agents.ports.session_identity import safe_session_id

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

ROOTS_DIRECTORY = "roots"

#: The largest record read: a path and its newline. A larger file is refused, not truncated.
MAXIMUM_RECORD_BYTES = 4096

_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
_CREATE = os.O_RDWR | os.O_CREAT | os.O_EXCL | _FLAGS
_READ = os.O_RDONLY | _FLAGS


class FileReportedRoots:
    """`ReportedRoots` as one 0600 file per session in a private directory; never raises."""

    def __init__(
        self, activity_directory: Path, *, accept: Callable[[object], Path | None]
    ) -> None:
        self._directory = activity_directory / ROOTS_DIRECTORY
        self._accept = accept

    def record(self, session_id: str, root: Path) -> bool:
        name = safe_session_id(session_id)
        if name is None:
            return False
        accepted = self._accept(str(root))
        if accepted is None:
            return False
        content = os.fsencode(accepted) + b"\n"
        if content.count(b"\n") != 1 or len(content) > MAXIMUM_RECORD_BYTES:
            return False
        directory = open_private_directory(self._directory)
        if directory is None:
            return False
        temporary = directory / f".pending-{os.urandom(6).hex()}"
        try:
            descriptor = os.open(temporary, _CREATE, 0o600)
        except OSError:
            return False
        try:
            try:
                written = 0
                while written < len(content):
                    written += os.write(descriptor, content[written:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, directory / name)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            return False
        return True

    def latest(self, session_id: str) -> Path | None:
        name = safe_session_id(session_id)
        if name is None or not _is_real_directory(self._directory):
            return None
        try:
            descriptor = os.open(self._directory / name, _READ)
        except OSError:
            return None
        try:
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != os.geteuid()
                or status.st_mode & 0o077
                or status.st_size > MAXIMUM_RECORD_BYTES
            ):
                return None
            content = os.pread(descriptor, MAXIMUM_RECORD_BYTES + 1, 0)
        except OSError:
            return None
        finally:
            os.close(descriptor)
        if len(content) > MAXIMUM_RECORD_BYTES or content.count(b"\n") != 1:
            return None
        if not content.endswith(b"\n"):
            return None
        return self._accept(os.fsdecode(content[:-1]))

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

    def forget(self, session_id: str) -> None:
        name = safe_session_id(session_id)
        if name is None or not _is_real_directory(self._directory):
            return
        try:
            os.unlink(self._directory / name)
        except OSError:
            return
