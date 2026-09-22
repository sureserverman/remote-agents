"""Every list lays its rows out to the region a row can actually use.

`content_size.width` counts the vertical scrollbar's cells. A list measured that way lays its
rows out two cells too wide the moment it scrolls, and `text-overflow: ellipsis` then cuts the
end of every row -- in the feed, the age. Found in the feed pane (0.46.0); the same read sat in
the sessions, launch and limits lists, so the property is asserted over the whole surface
rather than per list.
"""

from __future__ import annotations

from pathlib import Path

import remote_agents.adapters.tui as tui

_TUI = Path(tui.__file__).parent


def test_no_list_measures_its_rows_with_the_scrollbar_counted() -> None:
    offenders = [
        f"{path.relative_to(_TUI)}:{number}"
        for path in sorted(_TUI.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "content_size.width" in line
    ]
    assert not offenders, f"measure with `scrollable_content_region.width`: {offenders}"
