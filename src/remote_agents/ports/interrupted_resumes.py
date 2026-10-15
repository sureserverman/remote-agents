"""What became of each session a restart interrupted, so it is acted on once (DEC-127).

The startup resume pass launches sessions with nobody watching. Its decision is written down
against the interrupted session itself, so a second start -- or a start that follows a pass
killed part-way -- finds it decided and launches nothing (DEC-114).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

RESUMING = "resuming"
"""Written just before the resume is launched, and replaced by the outcome once it is known.

Not an outcome: an intent. A later start that finds it tries again, and the resume's
`restart:<session>` idempotency key decides: unclaimed means nothing was launched (a busy
refusal comes before the claim), claimed means the earlier launch got part-way and it is
given up as `skipped:interrupted`, never launched a second time.
"""

RESUMED = "resumed"
"""The prefix of `resumed:<new session id>`."""

SKIPPED = "skipped"
"""The prefix of `skipped:<reason>`."""


class InterruptedResumes(Protocol):
    """The `interrupted_resumes` table in the domain store."""

    async def outcomes(self) -> Mapping[str, str]:
        """Every row, as session id -> outcome (`RESUMING` included)."""
        ...

    async def record(self, session_id: str, outcome: str, *, decided_at: datetime) -> bool:
        """Write `outcome` for the session. The first outcome stands, except over a `RESUMING`
        intent, which it replaces. True when the row was written."""
        ...
