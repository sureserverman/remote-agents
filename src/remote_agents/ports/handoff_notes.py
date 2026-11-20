"""The note a session writes when the owner asks it to roll over with one (BL-119).

A session that runs no plan has no handoff envelope to write, so the owner's "Rollover with a
note" asks the agent itself for one: a short Markdown file at
`<root>/.claude/handoffs/<rollover id>.note.md`, beside the envelopes and under their
`.gitignore`. The service never reads what the note says. It checks only that the file is
there and is a plausible note -- a regular file of this user's, reached without a link, of
1 byte to 32 KiB -- and then types a fresh session the note's *path*, never its text.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Protocol

NOTE_ID = re.compile(r"n-[0-9a-f]{20}")
"""A note rollover's id, which names its note file; anything else names no note."""


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
    """The note files in a checkout's handoff folder."""

    def note_path(self, root: Path, rollover_id: str) -> Path | None:
        """Where the note of `rollover_id` is written; None for an id that names no note."""
        ...

    def prepare(self, root: Path) -> bool:
        """Make `<root>/.claude/handoffs/` with its `.gitignore` holding `*` where either is
        missing, refusing a link on the way. False when it could not."""
        ...

    def read(self, root: Path, rollover_id: str) -> NoteReading: ...

    def discard(self, root: Path, rollover_id: str) -> None:
        """Remove the note's name, never what a link there points at. Never raises."""
        ...
