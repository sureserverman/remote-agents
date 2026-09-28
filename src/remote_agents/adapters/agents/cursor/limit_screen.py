"""Cursor Agent's limit screen: the error it draws below its status line when a pool is spent.

Captured live on 2026.09.28-64d2043 (`tests/provider_contract/fixtures/cursor/limit_screen.txt`,
`docs/acceptance-2026-09-28-limit-screens.md`): `Error: Increase limits for faster responses`
then `You're out of usage. Switch to Auto, or ask your admin to increase your limit to
continue.`, with the unsent prompt left in the composer above. Cursor has no hooks, so the
limit-screen watch reads this off the pane.

**The window is the plan's month.** Cursor's included usage is monthly (`GetPlanInfo`:
`INCLUDED_USAGE_PERIOD_MONTHLY`) and the sentence names no instant, so the hint names the window
and leaves the reset to a reading.
"""

from __future__ import annotations

import re
from datetime import datetime

from remote_agents.ports.agent_activity import LimitHit
from remote_agents.ports.limit_screen import LimitScreen

#: Anchored at the start of a line (Cursor indents it two spaces), for Codex's reason.
_STOP = r"^\W*You're out of usage\."


def _hint(text: str, now: datetime) -> LimitHit | None:  # noqa: ARG001 -- no instant is named
    return LimitHit("month", None) if re.search(_STOP, text, re.MULTILINE) else None


LIMIT_SCREEN = LimitScreen(markers=(_STOP,), hint=_hint)
