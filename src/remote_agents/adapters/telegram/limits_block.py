"""The `Plan limits` block of the sessions list: `limit_rows`, laid out for a phone.

    Plan limits
    claude
    5h ███░░░░░  34% ↻ 2h
    wk █████░░░  61% ↻ 3d ▲ 8 over
    via status-line cache · live
    cursor-agent
    Cursor █████░░░  62% ↻ 12d
    other  ██░░░░░░  18% ↻ 12d
    via Cursor API · live
    codex
    no reading yet

The facts the terminal's limits pane draws, stacked one window per line so a line stays
within `WIDTH` monospace characters. Three things the pane has are left out for want of room
or of a medium: the `┃` pace tick, the `expected` figure, and colour. The parity contract
(`tests/frontend_contract/test_limits_parity.py`) hands one `limit_rows` result to this
renderer and to the terminal's and asks each for every fact in it.

**A function of the rows and nothing else.** Which agents have a row, what a percent rounds to
and when a reading is dated are `limit_rows`' decisions (DEC-043). What is here is the phone's
half: the layout and the words.

**Telegram has no colour**, so a threshold is carried by the bar and the number alone
(DEC-010: colour is only ever a second signal).

**Escaping happens here, at the boundary that decides the markup (DEC-014).** `limit_rows`
returns parts and takes no view on either surface's markup.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from remote_agents.application.session_views import (
    FIXED_LIMIT_WINDOWS,
    LimitRow,
    LimitWindow,
    countdown,
    percent_gauge,
)

TITLE = "Plan limits"

#: How many monospace characters a phone shows on one line of this block before it wraps.
#: The layout keeps to it for labels of two characters, which every kind a reader publishes has
#: here. A longer label -- a pool's name, a provider's own kind -- lengthens only its own
#: agent's lines, because the label column is padded within each agent (`_row_lines`).
WIDTH = 34

#: A paced kind as a phone line writes it, in two characters: the pace words need the room.
_WINDOW_LABELS = {"week": "wk", "day": "1d"}


def _label(kind: str) -> str:
    return _WINDOW_LABELS.get(kind, kind)


def _cell_label(kind: str, window: LimitWindow | None) -> str:
    """A pool's own name where `limit_rows` placed one (`Cursor`, `other`), else the kind's."""
    return window.name if window is not None and window.name else _label(kind)


def _pace_words(delta: int) -> str:
    """`on pace`, `▲ 8 over` or `▼ 3 under`: this surface's words for `pace_delta` (DEC-043)."""
    if delta == 0:
        return "on pace"
    return f"▲ {delta} over" if delta > 0 else f"▼ {-delta} under"


def _reset(row: LimitRow, window: LimitWindow) -> str:
    """` ↻ 2h`, or nothing where `countdown` says no surface may draw one."""
    left = countdown(row, window)
    return "" if left is None else f" ↻ {left}"


def _window_lines(row: LimitRow, label_width: int, percent_width: int) -> list[str]:
    """`5h ███░░░░░  34% ↻ 2h`: the fixed kinds first, then whatever else the row published.

    A fixed kind the row did not publish is its label and an empty bar (DEC-100).
    """
    published = {window.label: window for window in row.windows}
    kinds = FIXED_LIMIT_WINDOWS + tuple(
        kind for kind in published if kind not in FIXED_LIMIT_WINDOWS
    )
    lines = []
    for kind in kinds:
        window = published.get(kind)
        name = _cell_label(kind, window).ljust(label_width)
        if window is None:
            lines.append(f"{name} {percent_gauge(0)}")
            continue
        figure = f"{window.percent}%".rjust(percent_width)
        pace = "" if window.pace_delta is None else f" {_pace_words(window.pace_delta)}"
        lines.append(f"{name} {percent_gauge(window.percent)} {figure}{_reset(row, window)}{pace}")
    return lines


def _stamp(row: LimitRow) -> str:
    """`via status-line cache · live`: where the reading came from and how old it is (DEC-061)."""
    age = "live" if row.stale_for is None else f"as of {row.stale_for}"
    return age if row.borrowed is None else f"via {row.borrowed} · {age}"


def _row_lines(row: LimitRow, percent_width: int) -> list[str]:
    """One agent's lines, its labels padded to the widest *of its own*.

    Within the agent rather than across the message: Cursor's `Cursor` is six characters, and
    padded across the message it would push Claude's paced week line (`wk ░░░░░░░░   0% ↻ 23h
    ▼ 100 under`, exactly `WIDTH`) four characters past the phone's line. The cost is that
    Cursor's bars start four characters right of the others'.
    """
    if not row.windows:
        return [row.absence or ""]
    published = {window.label: window for window in row.windows}
    kinds = {*FIXED_LIMIT_WINDOWS, *published}
    label_width = max(len(_cell_label(kind, published.get(kind))) for kind in kinds)
    return [*_window_lines(row, label_width, percent_width), _stamp(row)]


def limits_block(rows: Sequence[LimitRow]) -> str:
    """The heading, then each agent's name over its lines; nothing at all for no rows.

    The heading is part of the block, so it goes when the block does: a bare `Plan limits`
    over nothing promises a block and delivers none.

    The percent is padded to a width measured across every row, so the figures of neighbouring
    agents end in one column; the label is padded within each agent (`_row_lines`).
    """
    if not rows:
        return ""
    windows = [window for row in rows for window in row.windows]
    percent_width = max((len(f"{window.percent}%") for window in windows), default=0)
    lines = [f"<b>{TITLE}</b>"]
    for row in rows:
        lines.append(escape(row.profile))
        lines.extend(f"<code>{escape(line)}</code>" for line in _row_lines(row, percent_width))
    return "\n".join(lines)
