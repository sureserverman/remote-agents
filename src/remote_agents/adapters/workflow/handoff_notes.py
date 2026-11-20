"""`HandoffNotes` over `<root>/.claude/handoffs/<id>.note.md` (BL-119).

Reached the way the envelope reader reaches its files (`handoff_envelopes.py`): every directory
below the root is opened `O_NOFOLLOW` relative to its parent's descriptor, and the note itself
is opened `O_NOFOLLOW | O_NONBLOCK`, so a link anywhere on the way is refused rather than
followed. The note's bytes are never read: only its type, owner and size are checked.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from remote_agents.adapters.workflow.handoff_envelopes import (
    CLAUDE_DIRECTORY,
    GITIGNORE,
    GITIGNORE_BODY,
    HANDOFF_DIRECTORY,
    _gitignore_is_ours,
    _handoff_directory,
    _replace,
)
from remote_agents.ports.handoff_notes import NOTE_ID, NoteReading

MAX_NOTE_BYTES = 32 * 1024
"""A note is a page of prose; a file past this is not one."""

_SUFFIX = ".note.md"
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC


class FileHandoffNotes:
    """The note files the agent writes when asked; see the port for what counts as one."""

    def note_path(self, root: Path, rollover_id: str) -> Path | None:
        if not NOTE_ID.fullmatch(rollover_id):
            return None
        return root / CLAUDE_DIRECTORY / HANDOFF_DIRECTORY / f"{rollover_id}{_SUFFIX}"

    def prepare(self, root: Path) -> bool:
        try:
            with _handoff_directory(root, create=True) as directory:
                if directory is None:
                    return False
                if not _gitignore_is_ours(directory):
                    _replace(directory, GITIGNORE, GITIGNORE_BODY)
        except Exception:
            return False
        return True

    def read(self, root: Path, rollover_id: str) -> NoteReading:
        if not NOTE_ID.fullmatch(rollover_id):
            return NoteReading.REFUSED
        try:
            with _handoff_directory(root, create=False) as directory:
                if directory is None:
                    return NoteReading.ABSENT
                try:
                    descriptor = os.open(f"{rollover_id}{_SUFFIX}", _READ, dir_fd=directory)
                except FileNotFoundError:
                    return NoteReading.ABSENT
                try:
                    info = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
        except OSError:
            # A link on the way (ELOOP), a directory at the note's name, a folder unreadable.
            return NoteReading.REFUSED
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or not 0 < info.st_size <= MAX_NOTE_BYTES
        ):
            return NoteReading.REFUSED
        return NoteReading.WRITTEN

    def discard(self, root: Path, rollover_id: str) -> None:
        if not NOTE_ID.fullmatch(rollover_id):
            return
        try:
            with _handoff_directory(root, create=False) as directory:
                if directory is not None:
                    os.unlink(f"{rollover_id}{_SUFFIX}", dir_fd=directory)
        except Exception:
            pass
