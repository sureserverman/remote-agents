"""The root each session's handoff writer last reported; technology-neutral."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path


class ReportedRoots(Protocol):
    """Each session's reported handoff root, one per session; never raises.

    Written by `remote-agents session-id` when its caller reports the git top level it writes
    envelopes under, and read by the service to find that session's envelopes. The same
    acceptance rule applies on write and on read, so a tampered record reads as none.
    """

    def record(self, session_id: str, root: Path) -> bool:
        """Make `root` the session's reported root; False when it was refused or not written."""
        ...

    def latest(self, session_id: str) -> Path | None:
        """The session's reported root, or None when none is recorded or it is not acceptable."""
        ...

    def sessions(self) -> tuple[str, ...]:
        """Every session holding a record."""
        ...

    def forget(self, session_id: str) -> None:
        """Remove the session's record."""
        ...
