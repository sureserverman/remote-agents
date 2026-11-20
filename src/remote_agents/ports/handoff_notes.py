"""The note a session writes when the owner asks it to roll over with one (BL-119).

A session that runs no plan has no handoff envelope to write, so the owner's "Rollover with a
note" asks the agent itself for one: a short Markdown file, `note.md`, in a folder made for
that one rollover in the session's folder, `<root>/.remote-agents-note-<rollover id>/`, beside
a `.gitignore` of `*` (OD-13). Not under `<root>/.claude/`, where Claude confirms its edit
tools' writes even under `acceptEdits` (OD-12). The service never reads what the note says. It
checks only that the file is there and is a plausible note -- a regular file of this user's,
reached without a link, of 1 byte to 32 KiB -- and then types a fresh session the note's
*path*, never its text. The folder goes when the rollover ends.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Protocol

NOTE_ID = re.compile(r"n-[0-9a-f]{20}")
"""A note rollover's id, which names its note's folder; anything else names no note."""


class NoteReading(StrEnum):
    """What the handoff folder holds for one note rollover."""

    ABSENT = "absent"
    """No file at the note's name yet."""
    REFUSED = "refused"
    """Something is there and it is not a note: a link, a directory, an empty or oversized
    file, another user's file. It is never accepted, however long it stays."""
    WRITTEN = "written"
    """A regular file of this user's, 1 byte to 32 KiB, reached without a link."""


class HandoffNotes(Protocol):
    """Each note rollover's folder in its session's folder, and the note in it."""

    def note_path(self, root: Path, rollover_id: str) -> Path | None:
        """Where the note of `rollover_id` is written; None for an id that names no note."""
        ...

    def prepare(self, root: Path, rollover_id: str) -> bool:
        """Make the rollover's own folder with its `.gitignore` holding `*` where either is
        missing, refusing a link on the way. False when it could not."""
        ...

    def read(self, root: Path, rollover_id: str) -> NoteReading: ...

    def discard(self, root: Path, rollover_id: str) -> None:
        """Remove the note, the `.gitignore` and then the folder if nothing else is in it --
        names only, never what a link there points at. Never raises."""
        ...
