"""`HandoffNotes` over `<root>/.remote-agents-note-<id>/note.md` (BL-119, OD-12, OD-13).

Each note rollover has a folder of its own in the session's folder, holding a `.gitignore` of
`*` (so git never sees the folder) and the agent's `note.md`. Not under `.claude/`: Claude
confirms its edit tools' writes there even under `acceptEdits` (measured 2026-11-20), so a note
asked for there is never written. Inside the session's folder, because Codex's sandbox writes
only there. The folder, its `.gitignore` and the note are removed when the rollover ends.

Reached without following a link: the root is opened `O_NOFOLLOW | O_DIRECTORY`, the folder
`O_NOFOLLOW` relative to the root's descriptor, and the note `O_NOFOLLOW | O_NONBLOCK`
relative to the folder's. The note's bytes are never read: only its type, owner and size.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from remote_agents.adapters.workflow.handoff_envelopes import (
    GITIGNORE,
    GITIGNORE_BODY,
    _gitignore_is_ours,
    _replace,
)
from remote_agents.ports.handoff_notes import NOTE_ID, NoteReading

MAX_NOTE_BYTES = 32 * 1024
"""A note is a page of prose; a file past this is not one."""

FOLDER_PREFIX = ".remote-agents-note-"
NOTE_NAME = "note.md"

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC


class FileHandoffNotes:
    """The note files the agent writes when asked; see the port for what counts as one."""

    def note_path(self, root: Path, rollover_id: str) -> Path | None:
        if not NOTE_ID.fullmatch(rollover_id):
            return None
        return root / f"{FOLDER_PREFIX}{rollover_id}" / NOTE_NAME

    def prepare(self, root: Path, rollover_id: str) -> bool:
        if not NOTE_ID.fullmatch(rollover_id):
            return False
        try:
            with _folder(root, rollover_id, create=True) as folder:
                if folder is None:
                    return False
                if not _gitignore_is_ours(folder):
                    _replace(folder, GITIGNORE, GITIGNORE_BODY)
        except Exception:
            return False
        return True

    def read(self, root: Path, rollover_id: str) -> NoteReading:
        if not NOTE_ID.fullmatch(rollover_id):
            return NoteReading.REFUSED
        try:
            with _folder(root, rollover_id, create=False) as folder:
                if folder is None:
                    return NoteReading.ABSENT
                try:
                    descriptor = os.open(NOTE_NAME, _READ, dir_fd=folder)
                except FileNotFoundError:
                    return NoteReading.ABSENT
                try:
                    info = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
        except OSError:
            # A link on the way (ELOOP, ENOTDIR), a folder unreadable.
            return NoteReading.REFUSED
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            # One name only: a hard link is a second name for a file written for something else.
            or info.st_nlink != 1
            or not 0 < info.st_size <= MAX_NOTE_BYTES
        ):
            return NoteReading.REFUSED
        return NoteReading.WRITTEN

    def discard(self, root: Path, rollover_id: str) -> None:
        if not NOTE_ID.fullmatch(rollover_id):
            return
        try:
            with _folder(root, rollover_id, create=False) as folder:
                if folder is None:
                    return
                try:
                    # unlink removes the name itself, never what a link there points at; a
                    # directory at the note's name is left, and so is its folder.
                    os.unlink(NOTE_NAME, dir_fd=folder)
                except FileNotFoundError:
                    pass
                except OSError:
                    return
                # The `.gitignore` goes only with the folder: while anything the agent left stays,
                # git must still not see it.
                with os.scandir(folder) as entries:
                    if any(entry.name != GITIGNORE for entry in entries):
                        return
                try:
                    os.unlink(GITIGNORE, dir_fd=folder)
                except FileNotFoundError:
                    pass
            root_descriptor = os.open(root, _DIRECTORY)
            try:
                # Only an empty folder goes: anything else the agent left there is the owner's.
                os.rmdir(f"{FOLDER_PREFIX}{rollover_id}", dir_fd=root_descriptor)
            finally:
                os.close(root_descriptor)
        except Exception:
            pass


@contextmanager
def _folder(root: Path, rollover_id: str, *, create: bool) -> Iterator[int | None]:
    """A descriptor for the rollover's folder, reached without following a link; None when it is
    missing and `create` is false. A made folder is `0o700` and must be this user's."""
    name = f"{FOLDER_PREFIX}{rollover_id}"
    try:
        root_descriptor = os.open(root, _DIRECTORY)
    except FileNotFoundError:
        if create:
            raise
        yield None  # no root, so no note: read as absent, never as refused
        return
    try:
        try:
            folder = os.open(name, _DIRECTORY, dir_fd=root_descriptor)
        except FileNotFoundError:
            if not create:
                yield None
                return
            try:
                os.mkdir(name, 0o700, dir_fd=root_descriptor)
            except FileExistsError:
                pass
            folder = os.open(name, _DIRECTORY, dir_fd=root_descriptor)
        try:
            if create and os.fstat(folder).st_uid != os.getuid():
                raise PermissionError(f"{name} is not owned by this user")
            yield folder
        finally:
            os.close(folder)
    finally:
        os.close(root_descriptor)
