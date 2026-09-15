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

from backends import SessionUseCaseDouble, backend_for
from rich.cells import cell_len
from textual.widgets import OptionList

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.rows import limit_rows_content
from remote_agents.adapters.tui.screens.dashboard import LimitsPaneScreen
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.remote_control_default import (
    REMOTE_CONTROL_DEFAULT_TITLE,
    remote_control_default_line,
)
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


# --- the Claude row, which is not a grid row and truncates rather than stacking -------------
#
# The two Remote Control lines at the foot of the pane are sentences, not rows of the table
# above them: nothing about them is columned, so `limit_row_content`'s stacked fallback does
# not apply and cannot. What happens to them instead is the pane's own `text-wrap: nowrap;
# text-overflow: ellipsis`, and that is worth pinning here rather than in the grid file for the
# reason this module exists at all -- it is the file about what a line does when it does not
# fit.
#
# Driven through the real surface because the truncation is the widget's, not the renderer's.
# The width is stated the same way `NARROW` is: measured, from the pane the owner actually has.

#: The limits pane's own content width at an 80-column terminal, which is where
#: `_HOST_CONNECTION_WORDS` measured its vocabulary: 28 cells, 23 of them spent before the
#: Codex reading begins. The Claude line is the longer of the two titles, so it is the one that
#: runs out of pane first -- `Claude Remote Control · unavailable` is 35 cells.
PANE_CELLS = 28


class _Launcher(SessionUseCaseDouble):
    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self) -> tuple[()]:
        return ()


def _narrow_context() -> TuiContext:
    """A host with no Claude provider, so the row renders its longest reading.

    `unavailable` rather than a wired `on`, deliberately: it is the widest of the four things
    this row can say, so a layout that survives it survives all of them.
    """
    return TuiContext(
        backend=backend_for(
            sessions=_Launcher(),  # type: ignore[arg-type]
            projects=object(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_NARROW_PROJECT,),
            catalogue=(_NARROW_PROJECT,),
        ),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


_NARROW_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")


async def test_the_claude_row_truncates_with_an_ellipsis_and_never_wraps() -> None:
    """One line, cut with an ellipsis -- not two lines, and not a line the pane cannot show.

    Both halves matter and they fail differently. A *wrapped* row costs the pane a line it was
    not sized for, and `_fit_to_content` counts one row per option -- so the continuation is
    drawn outside the pane's height and is unreachable, because every option here is disabled
    and no key scrolls to it. A row cut *without* an ellipsis is worse in the other direction:
    `Claude Remote Control · una` reads as a state rather than as a sentence that was cut.
    """
    full = remote_control_default_line(None)
    assert len(full) > PANE_CELLS, f"{full!r} fits {PANE_CELLS} cells, so nothing is truncated"

    app = RemoteAgentsTui(_narrow_context())
    async with app.run_test(size=(PANE_CELLS, 24)) as pilot:
        await app.push_screen(LimitsPaneScreen())
        await pilot.pause()
        await pilot.pause()
        pane = app.screen.query_one("#limits-pane", OptionList)
        painted = [pane.render_line(row).text for row in range(pane.size.height)]

        carrying = [line for line in painted if line.startswith(REMOTE_CONTROL_DEFAULT_TITLE)]
        assert len(carrying) == 1, f"the row wrapped instead of being cut: {painted}"

        (drawn,) = carrying
        drawn = drawn.rstrip()
        assert drawn.endswith("…"), f"cut without saying so: {drawn!r}"
        assert full.startswith(drawn[:-1]), f"{drawn!r} is not a prefix of {full!r}"
        assert cell_len(drawn) <= PANE_CELLS, f"{drawn!r} is {cell_len(drawn)} cells"

        drawn_rows = [line for line in painted if line.strip()]
        assert len(drawn_rows) == pane.option_count, (
            f"the pane paints {len(drawn_rows)} lines for {pane.option_count} rows, so one of "
            f"them wrapped: {painted}"
        )
