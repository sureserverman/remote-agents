"""The files a supervised plan executor leaves when it hands its work to a fresh session.

The planning plugin (coder-plugins' `handoff-envelope.py`) writes one small JSON envelope per
step of a handoff into `<project>/.claude/handoffs/`: `ready` when the old session has
written its resume point, `accepted` when a fresh session took it, `failed` with a closed
reason when it did not. This service reads them by project directory, which is what keeps
DEC-063 intact: no hook changes, and nothing here learns about a handoff from inside an
agent. The one file flowing the other way is `request.json`, by which this service asks a
managed session to hand off.

"Handoff" names the plugin's envelope and nothing else. What this service does with one --
launching a successor, stopping the predecessor -- is a *rollover*, and lives elsewhere.

The directory sits inside a checkout, so anything running there can write it. Every envelope
is untrusted until the adapter has checked it, and what reaches a caller is only what
passed: a typed value, never the raw file. What the adapter cannot check is whether
`managed_session_id` names a session of *this* project -- that needs the session store, and
the rollover pass that holds it makes that check.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

FAILURE_CODES = frozenset(
    {"id-mismatch", "no-ready", "cwd-mismatch", "branch-mismatch", "plan-missing"}
)
"""The closed set a `failed` envelope may name; anything else is not an envelope."""


class HandoffEvent(StrEnum):
    """Which step of a handoff an envelope records, spelled as on the wire."""

    READY = "HANDOFF_READY"
    ACCEPTED = "HANDOFF_ACCEPTED"
    FAILED = "HANDOFF_FAILED"


@dataclass(frozen=True, slots=True)
class HandoffEnvelope:
    """One envelope that passed every reading rule.

    `plan` is the path the writer recorded, carried as an opaque string: nothing on this side
    opens or interprets it. It is None only on a `failed` envelope, whose writer may not have
    had a plan to name. `failure_code` is set exactly when `event` is FAILED, and is one of
    `FAILURE_CODES`.
    """

    event: HandoffEvent
    handoff_id: str
    managed_session_id: str
    timestamp: datetime
    plan: str | None
    failure_code: str | None


class HandoffEnvelopes(Protocol):
    """Read a project's handoff envelopes and write or clear its request; never raises."""

    def events(self, project_dir: Path) -> tuple[HandoffEnvelope, ...]:
        """Every valid envelope in the project, oldest first; anything refused is left out.
        A project with no envelope directory has none."""
        ...

    def write_request(self, project_dir: Path, managed_session_id: str) -> bool:
        """Ask the managed session `managed_session_id` to hand off. Returns whether the
        request was written; a refusal writes nothing."""
        ...

    def requested(self, project_dir: Path) -> str | None:
        """The session the project's `request.json` names, when it holds a well-formed
        request; None otherwise. Never raises."""
        ...

    def discard(self, project_dir: Path, handoff_id: str) -> None:
        """Remove a finished handoff's ready, accepted and failed envelopes; never raises.
        Called once its rollover is terminal, so the directory does not fill over time."""
        ...

    def clear_request(self, project_dir: Path, managed_session_id: str) -> None:
        """Withdraw the project's request if it names `managed_session_id`; nothing otherwise.
        Compared first, so ending one session's rollover never clears another's request."""
        ...
