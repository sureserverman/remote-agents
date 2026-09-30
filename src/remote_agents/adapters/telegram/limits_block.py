"""The `Plan limits` block of the sessions list: `limit_rows`, laid out for a phone.

A function of the rows and nothing else, so the parity contract
(`tests/frontend_contract/test_limits_parity.py`) can hand one `limit_rows` result to this
renderer and to the terminal's and compare what each says.

**Escaping happens here, at the boundary that decides the markup (DEC-014).** `limit_rows`
returns parts and takes no view on either surface's markup (DEC-043).
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from remote_agents.application.session_views import LimitRow

TITLE = "Plan limits"


def limits_block(rows: Sequence[LimitRow]) -> str:
    """The block's lines under its heading, or nothing at all for no rows.

    The heading is part of the block, so it goes when the block does: a bare `Plan limits`
    over nothing promises a block and delivers none.
    """
    if not rows:
        return ""
    width = max(len(row.profile) for row in rows) + 2
    lines = []
    for row in rows:
        pieces = [f"{window.label} {window.percent}%" for window in row.windows]
        if row.borrowed is not None:
            pieces.append(f"via {row.borrowed}")
        if row.stale_for is not None:
            pieces.append(f"as of {row.stale_for} ago")
        lines.append(f"<code>{escape(row.profile.ljust(width))}{escape(' · '.join(pieces))}</code>")
    return f"<b>{TITLE}</b>\n" + "\n".join(lines)
