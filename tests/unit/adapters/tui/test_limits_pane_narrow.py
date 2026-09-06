"""The limits pane's narrow fallback: one window per line, and never half a gauge.

The dashboard's right column is a `2fr` slice of a `3fr/2fr` split, which at this project's
100-column baseline leaves about 38 cells — and two windows with reset countdowns run to 46.
So `limit_row_content` has always had a second layout: where the one-line row does not fit,
each window takes a line of its own under the padded profile and the trailers take a line
after those.

The pane draws `nowrap`, so **what `limit_row_content` returns is literally what the owner
sees**. Nothing downstream will re-wrap a line that is too long; it is simply cut. That makes
two properties load-bearing rather than cosmetic:

* **A gauge is never split across two lines.** Half an eight-cell bar reads as a different
  figure, not as a truncated one, and the owner has no way to tell which.
* **The borrowed-source stamp and the staleness date are never the part that falls off.** They
  are what stops a stale number being read as current, so a layout that drops them under
  pressure is worse than one that drops a countdown.

**This file exists as its own module rather than as more cases in `test_limits_pane_grid.py`
because the two layouts are different contracts and mixing them lets a regression in either
hide behind the other.** The grid file asserts that rows agree on their columns, which is a
statement about a *set of rows on one line each*; this file asserts what happens when that
line cannot exist.

**The trailing-whitespace assertion is not tidiness.** The grid work of 2026-09-06 pads a
window's reset field so the window that follows it starts in a fixed column. On the narrow
layout nothing follows it — the next window is on the next line — so that padding becomes
trailing spaces, and trailing spaces are counted by `cell_length`. Left in, they inflate the
measurement that decides whether the trailer fits beside the last window, and the trailer gets
pushed onto a line of its own on the strength of spaces nobody can see.
"""

from __future__ import annotations

import re

from remote_agents.adapters.tui.rows import limit_rows_content
from remote_agents.application.session_views import LimitRow, LimitWindow

#: Narrower than two windows with countdowns need, and close to the dashboard's own right
#: column. Measured rather than picked: `limit_row_content`'s docstring records that region as
#: 38 cells at the 100-column baseline, and that two windows with countdowns run to 46.
NARROW = 38

#: The gauge is eight cells, always (`session_views._GAUGE_CELLS`). A line carrying part of one
#: is the failure this file's first assertion is about.
GAUGE_CELLS = 8

_GAUGE_RUN = re.compile(r"[█░]+")


def _stale_row() -> LimitRow:
    """An agent whose reading is old and borrowed — the row with the most to say.

    Stale, so every countdown is suppressed and the trailer carries the date instead; two
    windows, so the narrow layout has something to break across lines.
    """
    return LimitRow(
        profile="claude",
        windows=(LimitWindow("5h", 34, "2h"), LimitWindow("week", 61, "3d")),
        borrowed="a-cache-file",
        stale_for="2h",
    )


def _fresh_row() -> LimitRow:
    return LimitRow(
        profile="codex",
        windows=(LimitWindow("5h", 3, "45m"), LimitWindow("day", 100, "6h")),
        borrowed=None,
        stale_for=None,
    )


def test_a_narrow_pane_gives_each_window_its_own_line() -> None:
    """Two windows that cannot share a line are stacked, not truncated."""
    lines = [content.plain for content in limit_rows_content((_fresh_row(),), NARROW)]
    assert len(lines) >= 2, f"expected the stacked layout at {NARROW} cells, got {lines}"

    carrying = [line for line in lines if _GAUGE_RUN.search(line)]
    assert len(carrying) == 2, f"one line per window, got {len(carrying)}: {lines}"


def test_no_narrow_line_ever_carries_part_of_a_gauge() -> None:
    """Every gauge on the page is a whole one.

    Swept over both rows and every line rather than checked on the one that happened to
    overflow: a bar cut to six cells reads as a different figure, and which line it lands on
    is a function of the profile id's length.
    """
    lines = [content.plain for content in limit_rows_content((_stale_row(), _fresh_row()), NARROW)]
    for line in lines:
        for run in _GAUGE_RUN.finditer(line):
            assert len(run.group()) == GAUGE_CELLS, (
                f"a gauge of {len(run.group())} cells, not {GAUGE_CELLS}, in: {line!r}"
            )


def test_the_staleness_stamp_survives_the_narrow_layout() -> None:
    """A stale reading still says it is stale, whatever the width.

    The countdown is the part a narrow pane may lose; the date is not. It is what stops a
    number read two hours ago being read as current.
    """
    lines = [content.plain for content in limit_rows_content((_stale_row(),), NARROW)]
    assert any("as of 2h" in line for line in lines), lines


def test_no_narrow_line_ends_in_the_padding_that_aligns_the_wide_one() -> None:
    """A stacked line carries no trailing spaces.

    The reset field is padded so that on the *wide* layout the next window starts in a fixed
    column. On this layout the next window is on the next line, so the padding aligns nothing
    and only inflates `cell_length` — which is the measurement deciding whether the trailer
    fits beside the last window. Left in, the trailer is pushed onto a line of its own by
    invisible spaces.
    """
    lines = [content.plain for content in limit_rows_content((_stale_row(), _fresh_row()), NARROW)]
    trailing = [line for line in lines if line != line.rstrip()]
    assert not trailing, f"lines ending in padding: {trailing!r}"


def test_the_wide_layout_is_still_one_line_per_agent() -> None:
    """The fallback is a fallback — given room, each agent is one row.

    Guards the repair direction: making the narrow case correct by always stacking would pass
    every assertion above and destroy the table the grid work just built.
    """
    lines = limit_rows_content((_stale_row(), _fresh_row()), 120)
    assert len(lines) == 2, [content.plain for content in lines]


def test_one_render_never_mixes_the_two_layouts() -> None:
    """Either every agent is a row, or every agent is a stack — never some of each.

    A gate-evaluator Material finding. The fit decision used to be taken per row against that
    row's own length, which left a band of widths — seven columns wide — where a short row
    stayed on one line while a longer one stacked. Inside a single render one agent's second
    window then sat at column 30 and another's at column 8, which is this stage's own goal
    failing on screen.

    The routine data hits it: Claude's borrowed reading goes stale behind a thirty-minute
    fence while Codex's does not, so one row carries countdowns and the other carries a date,
    and the two rows differ in length by more than the band is wide. The layout is a property
    of the table, so it is decided once from the widest row.
    """
    rows = (_stale_row(), _fresh_row())
    for width in range(40, 80):
        lines = [content.plain for content in limit_rows_content(rows, width)]
        carrying = [line for line in lines if _GAUGE_RUN.search(line)]
        gauges = [len(_GAUGE_RUN.findall(line)) for line in carrying]
        assert len(set(gauges)) == 1, (
            f"at width {width} the render mixes layouts — rows carry {gauges} windows each:\n"
            + "\n".join(lines)
        )
