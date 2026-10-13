"""The roster a payload-identified hook names its session from (DEC-126).

Written by the service, read by the hook in another process. The application decides who is on
it; the adapter decides where and how it is stored (`adapters/agents/hook_identity`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RosterSession:
    """One live managed session, as a hook needs it to recognise its own payload."""

    session_id: str
    workspace: Path
    created_at: datetime
    resume_source_id: str | None = None


class HookRosterWriter(Protocol):
    """Replace one hook's roster with exactly these sessions."""

    def write(self, hook_name: str, sessions: Sequence[RosterSession]) -> None: ...
