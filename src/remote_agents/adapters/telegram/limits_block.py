"""The `Plan limits` block of the sessions list: `limit_rows`, laid out for a phone.

    Plan limits
    claude
    5h ███░░░░░  34% ↻ 2h
    wk █████░░░  61% ↻ 3d ▲ 8 over
    via status-line cache · live
    cursor-agent
    █████░░░│██░░░░░░ ↻ 12d
    Cursor 62% · other 18%
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
    part_figures,
    percent_gauge,
    split_window,
)

TITLE = "Plan limits"

#: How many monospace characters a phone shows on one line of this block before it wraps.
#: The layout keeps to it for labels of two characters, which every kind a reader publishes
#: outside a split row has here. A longer provider label lengthens its block's lines by the
#: difference.
WIDTH = 34

#: A paced kind as a phone line writes it, in two characters: the pace words need the room.
_WINDOW_LABELS = {"week": "wk", "day": "1d"}

#: What divides the pools of a split bar.
_PART_DIVIDER = "│"


def _label(kind: str) -> str:
    return _WINDOW_LABELS.get(kind, kind)


def _pace_words(delta: int) -> str:
    """`on pace`, `▲ 8 over` or `▼ 3 under`: this surface's words for `pace_delta` (DEC-043)."""
    if delta == 0:
        return "on pace"
    return f"▲ {delta} over" if delta > 0 else f"▼ {-delta} under"


def _reset(row: LimitRow, window: LimitWindow) -> str:
    """` ↻ 2h`, or nothing where `countdown` says no surface may draw one."""
    left = countdown(row, window)
    return "" if left is None else f" ↻ {left}"


def _split_lines(row: LimitRow, window: LimitWindow) -> list[str]:
    """One gauge per pool, side by side, then each pool's own percent on the line under it.

    Two lines, because bar and figures together outrun `WIDTH`. Never one fill: each percent
    is of its own pool, so a bar summing them would draw a total the provider does not publish.
    """
    bar = _PART_DIVIDER.join(percent_gauge(part.percent) for part in window.parts)
    return [f"{bar}{_reset(row, window)}", " · ".join(part_figures(window))]


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
        name = _label(kind).ljust(label_width)
        window = published.get(kind)
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


def _row_lines(row: LimitRow, label_width: int, percent_width: int) -> list[str]:
    if not row.windows:
        return [row.absence or ""]
    split = split_window(row)
    if split is not None:
        return [*_split_lines(row, split), _stamp(row)]
    return [*_window_lines(row, label_width, percent_width), _stamp(row)]


def limits_block(rows: Sequence[LimitRow]) -> str:
    """The heading, then each agent's name over its lines; nothing at all for no rows.

    The heading is part of the block, so it goes when the block does: a bare `Plan limits`
    over nothing promises a block and delivers none.

    The label and the percent are padded to a width measured across every row, so the bars of
    neighbouring agents start in one column and the figures end in one.
    """
    if not rows:
        return ""
    laid_out = [row for row in rows if row.windows and split_window(row) is None]
    windows = [window for row in laid_out for window in row.windows]
    kinds = {*FIXED_LIMIT_WINDOWS, *(window.label for window in windows)}
    label_width = max(len(_label(kind)) for kind in kinds)
    percent_width = max((len(f"{window.percent}%") for window in windows), default=0)
    lines = [f"<b>{TITLE}</b>"]
    for row in rows:
        lines.append(escape(row.profile))
        lines.extend(
            f"<code>{escape(line)}</code>" for line in _row_lines(row, label_width, percent_width)
        )
    return "\n".join(lines)
