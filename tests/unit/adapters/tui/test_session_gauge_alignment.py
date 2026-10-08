"""The sessions pane's context shares line up on their right edge (BL-047).

The limits pane right-aligns its percents, because a column of figures is compared down its
right edge. The sessions gauge used to pad its share on the right instead, so `9%`, `56%` and
`100%` ended in three different columns.
"""

from __future__ import annotations

from remote_agents.adapters.tui.rows import gauge_content


def test_shares_of_every_width_end_in_the_same_column() -> None:
    gauges = [gauge_content(gauge) for gauge in ("█░░░░░░░ 9%", "█████░░░ 56%", "████████ 100%")]

    assert {gauge.plain.index("%") for gauge in gauges} == {12}
    assert {gauge.cell_length for gauge in gauges} == {13}


def test_a_bar_less_count_is_drawn_as_before() -> None:
    assert gauge_content("185k").plain == "185k "
