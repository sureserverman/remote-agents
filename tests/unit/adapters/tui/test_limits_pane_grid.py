"""The limits pane is a table, so a reader can compare two agents down a column.

The pane draws one row per agent that publishes windows, and the owner's question of it is
almost always comparative: *which of these is closer to its ceiling.* That question is answered
by scanning down a column, and a column only exists if every row puts its Nth window in the
same place.

Until this file, none did. `limit_row_content` padded the **profile** to the widest profile id
and then appended each window with a bare `Content("  ")` join, so every cell after the first
started wherever the previous one happened to end. Three things vary between rows and all three
move the boundary:

* the **label** — Claude publishes `5h` and `week` (rendered `wk`), Codex derives its own from
  `window_minutes` and can produce `day`, so the label is two or three cells depending on the
  agent and the window;
* the **percent** — `3%`, `34%` and `100%` are two, three and four cells;
* the **reset countdown** — `↻ 2h` is present only when the provider published a reset *and*
  the reading is fresh, so one row can carry it where the row above does not.

Stacked, a `codex  5h ███░░░░░ 3%` row and a `claude 5h ███░░░░░ 34% ↻ 2h` row put their
second window four cells apart, and the pane reads as two unrelated sentences rather than as a
table.

**The sessions pane already solved this and is the model, not a new idea.** `session_contents`
measures `state_width`, `age_width` and `gauge_width` across *every row of the listing* and
hands those widths to each row's `columns()` call, so the columns are a property of the set
rather than of whichever row is drawn first. `feed_rows` does the same with `kind_width` and
`age_width`. The limits pane is the one list in the surface that never learned the trick.

**What this file asserts is offsets, not appearance.** Two rows agreeing on where their gauges
start and where their percents end is what "tabulated" means operationally, and it is a
property of the whole rendered set — so the assertions quantify over every pair of rows and
every window index, rather than checking the two rows a screenshot happened to contain.

The locator is deliberately structural. It finds each window by its gauge run (`█`/`░`, the
one glyph pair `percent_gauge` emits and nothing else in the row does) rather than by
searching for a label, because a label like `day` is three ordinary letters that could appear
inside a profile id and a locator that can match the wrong thing turns a real failure into a
confusing one.
"""

from __future__ import annotations

import re

import pytest

from remote_agents.adapters.tui.rows import limit_rows_content
from remote_agents.application.session_views import LimitRow, LimitWindow

#: Wide enough that `limit_row_content` takes its one-line branch for every row here. The
#: narrow branch is a different layout with its own contract and its own test
#: (`test_limits_pane_narrow.py`); mixing the two in one assertion would let a regression in
#: either hide behind the other.
WIDE = 120

#: The two glyphs `percent_gauge` emits, and the only place in a limits row they occur.
_GAUGE = re.compile(r"[█░]+")


def _gauge_spans(line: str) -> list[tuple[int, int]]:
    """Where each window's gauge starts and ends, in cells, left to right."""
    return [(match.start(), match.end()) for match in _GAUGE.finditer(line)]


def _percent_ends(line: str) -> list[int]:
    """Where each window's percent figure ends, in cells.

    Measured from the gauge rather than by searching for digits: a profile id may contain
    digits and a reset countdown certainly does, so `line.index("34%")` is a locator that can
    land in the wrong window. The percent is the run immediately after a gauge, so its end is
    the end of the first `%` following that gauge.
    """
    ends = []
    for _start, gauge_end in _gauge_spans(line):
        marker = line.index("%", gauge_end)
        ends.append(marker + 1)
    return ends


def _rows() -> tuple[LimitRow, ...]:
    """Two agents whose windows differ in every dimension that moves a column boundary.

    Not invented: these are the shapes the two installed providers actually produce.
    `claude/usage.py` publishes `("five_hour", "5h")` and `("seven_day", "week")`;
    `codex/usage.py` derives its label from `window_minutes` and can emit `day`. The percents
    span all three widths a whole percent can take, and only one agent's reading carries reset
    countdowns — which is the routine case, since a countdown is dropped whenever the reading
    is stale.
    """
    return (
        LimitRow(
            profile="claude",
            windows=(
                LimitWindow("5h", 34, "2h"),
                LimitWindow("week", 61, "3d"),
            ),
            borrowed=None,
            stale_for=None,
        ),
        LimitRow(
            profile="codex",
            windows=(
                LimitWindow("5h", 3, None),
                LimitWindow("day", 100, None),
            ),
            borrowed=None,
            stale_for=None,
        ),
    )


def test_every_row_starts_its_nth_window_in_the_same_column() -> None:
    """The gauge of window N begins at one offset, whichever agent's row it is drawn on."""
    lines = [content.plain for content in limit_rows_content(_rows(), WIDE)]
    assert len(lines) == len(_rows()), "each row should occupy exactly one line at this width"

    per_row = [_gauge_spans(line) for line in lines]
    assert {len(spans) for spans in per_row} == {2}, "both agents publish two windows"

    for index in range(2):
        starts = {spans[index][0] for spans in per_row}
        assert len(starts) == 1, (
            f"window {index} starts at {sorted(starts)} across rows; a column is one offset.\n"
            + "\n".join(lines)
        )


def test_every_row_ends_its_nth_percent_in_the_same_column() -> None:
    """A percent is read by comparing figures, so the figures share a right edge.

    Separate from the gauge assertion because the two fail for different repairs: aligning the
    starts alone still lets `3%` and `100%` push the following cell apart, which is the defect
    one column over rather than the defect fixed.
    """
    lines = [content.plain for content in limit_rows_content(_rows(), WIDE)]
    per_row = [_percent_ends(line) for line in lines]

    for index in range(2):
        ends = {ends[index] for ends in per_row}
        assert len(ends) == 1, (
            f"window {index}'s percent ends at {sorted(ends)} across rows.\n" + "\n".join(lines)
        )


@pytest.mark.parametrize(
    "profiles",
    [
        ("claude", "codex"),
        ("codex", "claude"),
        ("a", "much-longer-profile-id"),
    ],
    ids=["claude-first", "codex-first", "lopsided-names"],
)
def test_the_columns_are_a_property_of_the_set_not_of_the_first_row(
    profiles: tuple[str, str],
) -> None:
    """Reordering the rows, or widening one profile id, moves every row's columns together.

    The failure this guards is a fix that measures the widths from `rows[0]` and pads the rest
    to it. That passes the two assertions above on any input whose first row happens to be the
    widest, and fails the moment the order changes — which is exactly what an agent dropping
    out of the read does. `session_contents` takes the max across the listing for this reason.
    """
    first, second = _rows()
    reordered = (
        LimitRow(profiles[0], first.windows, first.borrowed, first.stale_for),
        LimitRow(profiles[1], second.windows, second.borrowed, second.stale_for),
    )
    lines = [content.plain for content in limit_rows_content(reordered, WIDE)]
    per_row = [_gauge_spans(line) for line in lines]

    for index in range(2):
        starts = {spans[index][0] for spans in per_row}
        assert len(starts) == 1, (
            f"window {index} starts at {sorted(starts)} for {profiles}.\n" + "\n".join(lines)
        )


# --- gate remediation, 2026-09-06 -----------------------------------------------------------
#
# Three cases the first round did not cover, two of them Material findings from the stage
# gate's evaluator and one a Tier-2 suggestion. Each is a shape real data takes and the
# original fixture did not.


def test_rows_with_different_window_counts_still_agree_on_window_zero() -> None:
    """One agent publishing one window beside another publishing three.

    The shape where an off-by-one in `_window_content`'s `last=` would first show: the
    single-window row's only window IS its last and so goes unpadded, while the three-window
    row's first is padded. Nothing follows the short row's window, so the suppression cannot
    propagate — but that is an argument, and this is the test that makes it checkable.
    """
    rows = (
        LimitRow("solo", (LimitWindow("5h", 7, "1h"),), None, None),
        LimitRow(
            "trio",
            (
                LimitWindow("5h", 100, "2h"),
                LimitWindow("week", 3, None),
                LimitWindow("day", 44, "9h"),
            ),
            None,
            None,
        ),
    )
    lines = [content.plain for content in limit_rows_content(rows, WIDE)]
    per_row = [_gauge_spans(line) for line in lines]
    assert [len(spans) for spans in per_row] == [1, 3], lines

    assert len({spans[0][0] for spans in per_row}) == 1, f"window 0 misaligned:\n" + "\n".join(
        lines
    )
    assert len({_percent_ends(line)[0] for line in lines}) == 1, "\n".join(lines)


def test_one_long_profile_id_does_not_cost_every_row_its_gauge() -> None:
    """The profile column is a max across rows, so an outlier name degrades the whole table.

    Uncapped, a 23-character profile at a 34-cell pane produced lines longer than the pane and
    the widget's `text-overflow: ellipsis` then ate the gauge — leaving a column of names with
    no figure beside any of them. The name is the row's handle and the window is its payload,
    so it is the name that gives way.
    """
    rows = (
        LimitRow("claude-personal-max-20x", (LimitWindow("5h", 4, None),), None, None),
        LimitRow("codex", (LimitWindow("5h", 9, None),), None, None),
    )
    for width in (34, 28, 22):
        lines = [content.plain for content in limit_rows_content(rows, width)]
        for line in lines:
            assert _GAUGE.search(line), f"at width {width} a row lost its gauge: {line!r}"
            assert len(line) <= width, f"at width {width}: {line!r} is {len(line)} cells"
        assert any("…" in line for line in lines), (
            f"at width {width} the name should ellipsise rather than the data: {lines}"
        )
