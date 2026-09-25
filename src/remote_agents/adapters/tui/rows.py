"""How the local surface draws a row: a glyph, aligned columns, colour through theme variables.

Every list on this surface -- sessions, projects, plan limits, the feed -- is an `OptionList`
whose rows are `Content`. `Content` rather than a string because a row is coloured per field
(the state word in its group's colour, the age muted, the gauge's fill in `$primary`), and rather
than `rich.text.Text` because a Rich style names a concrete colour while a Content style may
name a **theme variable**, which is the only way a row can follow the theme without this module
knowing which one is in force.

**Nothing here parses markup.** Every piece of text arrives through `Content(text)` or a
`(text, style)` pair in `Content.assemble`, so an owner's label or an agent's words containing
`[bold]` are drawn as those five characters -- the property `markup=False` on the widgets already
holds, kept here for the same reason (`tests/unit/adapters/tui/test_row_markup.py`).

**Columns are laid out to a measured width, not to CSS.** An `OptionList` row is one line of
content with no grid inside it, so a right-aligned age column exists only if the row is padded to
the pane's width. `columns` does that: one flexible cell takes what the fixed ones leave and is
ellipsised, the rest are padded to the widest member of their column. A pane that has not been
laid out yet reports width 0, and the row is then joined without padding -- the same fallback the
feed's continuation rows already make.

The decisions -- which word, which group, whether a gauge is drawn -- are `application/
session_views.py`'s. This module only places and colours what it is handed (DEC-043).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from textual.content import Content

from remote_agents.application.session_views import (
    LimitRow,
    SessionRowParts,
    StateGroup,
    group_counts,
    percent_gauge,
)
from remote_agents.domain.models import SessionRecord
from remote_agents.ports.agent_activity import ActivityKind

MUTED = "$text-muted"
DIM = "$text-dim"

GROUP_STYLE: dict[StateGroup, str] = {
    StateGroup.ACTIVE: "$success",
    StateGroup.IN_TRANSITION: "$warning",
    StateGroup.NEEDS_ATTENTION: "$error",
    StateGroup.PRESERVED: MUTED,
}
"""The colour of a bucket's glyph and state word. Colour is the second signal; the glyph and
the word are the first, and they survive `NO_COLOR` unchanged (DEC-010)."""

GROUP_GLYPH: dict[StateGroup, str] = {
    StateGroup.ACTIVE: "●",
    StateGroup.IN_TRANSITION: "●",
    StateGroup.NEEDS_ATTENTION: "●",
    StateGroup.PRESERVED: "○",
}
"""A filled dot for anything that is or was live and an open one for a pane kept only for
reading -- the one distinction a monochrome terminal can still make between the four."""

GROUP_STATUS_WORD: dict[StateGroup, str] = {
    StateGroup.ACTIVE: "running",
    StateGroup.IN_TRANSITION: "starting",
    StateGroup.NEEDS_ATTENTION: "attention",
    StateGroup.PRESERVED: "preserved",
}
"""How the status line's count names each bucket: `● 2 running · ● 1 starting · …`."""

KIND_GLYPH: dict[ActivityKind, str] = {
    ActivityKind.NEEDS_ANSWER: "?",
    ActivityKind.COMPLETED: "✓",
    ActivityKind.LIMIT_REACHED: "!",
    ActivityKind.OUTPUT_LIMIT: "!",
}

KIND_STYLE: dict[ActivityKind, str] = {
    ActivityKind.NEEDS_ANSWER: "$accent",
    ActivityKind.COMPLETED: "$success",
    ActivityKind.LIMIT_REACHED: "$warning",
    ActivityKind.OUTPUT_LIMIT: "$warning",
}

#: A feed row older than this is drawn entirely muted: it is history, and the eye should land
#: on what is recent.
FEED_HISTORY_AGE = timedelta(hours=24)

#: How many cells the identity-and-detail cell must be left before the kind *word* is worth its
#: column. Below it the glyph stands alone for the kind, so project, agent and sequence stay on
#: screen at the dashboard's narrow feed region.
FEED_NARROW_ROOM = 24

#: The gauge column's floor. `████████ 100%` is thirteen cells and an understated Claude ceiling
#: renders wider still (the percent is deliberately unclamped), so this is a floor and the
#: column grows to its widest member.
GAUGE_COLUMN = 12

NO_GAUGE = "—"

#: The mark on the row the sessions positions' keys act on -- the *active* session, which since
#: the cursor and the target were split is not always the row under the cursor.
#:
#: **A glyph and not only the colour, for the reason every other signal on this surface carries
#: one** (DEC-010, and the note on `GROUP_STYLE`): under `NO_COLOR` the yellow identity below is
#: byte-identical to every other row, and what it names is which session an unconfirmed `s` will
#: end. The column is one cell and is reserved for every row of a listing that marks an active
#: session, so the rows do not shift by two cells the moment nothing is active.
ACTIVE_MARKER = "▸"

#: The colour of the active row's marker and identity. `$warning` is the surface's yellow, read
#: through the theme like every other colour here rather than written as a literal.
ACTIVE_STYLE = "$warning"

_GUTTER = 1


def text(value: str, style: str | None = None) -> Content:
    """One piece of literal text, optionally styled. Never parsed as markup."""
    return Content.assemble((value, style)) if style else Content(value)


def columns(
    cells: Sequence[tuple[Content, int | None]], width: int | None, *, flexible: int
) -> Content:
    """Lay cells out on one line: fixed cells padded to their width, one cell taking the rest.

    `cells` are `(content, column_width)` pairs; a `None` width is the cell's own length. The
    cell at index `flexible` is padded or ellipsised to whatever `width` leaves after the others
    and their gutters. With no usable width the cells are simply joined, which is what a pane
    reports before its first layout.
    """
    if width is None or width <= 0:
        return _join(cell for cell, _width in cells)
    fixed = sum(
        (cell.cell_length if column is None else column)
        for index, (cell, column) in enumerate(cells)
        if index != flexible
    )
    room = width - fixed - _GUTTER * (len(cells) - 1)
    if room < 4:
        return _join(cell for cell, _width in cells)
    placed: list[Content] = []
    for index, (cell, column) in enumerate(cells):
        if index == flexible:
            placed.append(cell.truncate(room, ellipsis=True, pad=True))
            continue
        target = cell.cell_length if column is None else column
        placed.append(cell.truncate(target, ellipsis=True, pad=True))
    return _join(placed)


def _join(cells) -> Content:
    joined = Content("")
    gutter = Content(" " * _GUTTER)
    for index, cell in enumerate(cells):
        if index:
            joined = joined + gutter
        joined = joined + cell
    return joined


# --- sessions -----------------------------------------------------------------------------


def session_glyph(group: StateGroup) -> Content:
    return text(GROUP_GLYPH[group], GROUP_STYLE[group])


def gauge_content(gauge: str | None) -> Content:
    """The bar with its fill in `$primary` and its track in `$secondary`, then the share; or an
    em dash, dim, where `session_row_parts` decided no bar is drawn."""
    if gauge is None:
        return text(NO_GAUGE, DIM)
    bar, _space, share = gauge.partition(" ")
    filled = bar.rstrip("░")
    empty = bar[len(filled) :]
    return Content.assemble((filled, "$primary"), (empty, "$secondary"), (f" {share}", None))


def session_content(
    parts: SessionRowParts,
    *,
    width: int | None,
    state_width: int,
    age_width: int,
    gauge_width: int,
    active: bool | None = None,
) -> Content:
    """One session row: `glyph identity #n  state  age  gauge`, the identity taking the slack.

    A preserved row's identity is muted whole, not just its glyph -- the row is there to be
    found, not to be scanned past what is live. The sequence is always muted: it is the handle
    the row keys act on, and the name is what the eye reads first.

    `active` has **three** values and the third is not a `False` in disguise. `None` means this
    listing marks no active session at all and the marker column is not drawn -- the dashboard's
    sessions region, which binds none of the row keys and so has nothing to mark. `True` and
    `False` both reserve the column, so a listing that marks one does not reflow by two cells
    the moment nothing is active.

    The active row's **identity** takes `ACTIVE_STYLE` rather than the whole row taking it: the
    state word keeps its group's colour, which is the row's other signal and the one a stop key
    is about to be checked against. It overrides the preserved mute deliberately -- a preserved
    row that is the target of `c` is not a row to be scanned past.
    """
    if active:
        identity_style: str | None = ACTIVE_STYLE
    elif parts.group is StateGroup.PRESERVED:
        identity_style = MUTED
    else:
        identity_style = None
    identity = Content.assemble((parts.identity, identity_style), (f" #{parts.sequence}", MUTED))
    if parts.note:
        identity = identity + Content.assemble((f" · {parts.note}", MUTED))
    cells = [
        (session_glyph(parts.group), 1),
        (identity, None),
        (text(parts.state, GROUP_STYLE[parts.group]), state_width),
        (text(parts.age, MUTED), age_width),
        (gauge_content(parts.gauge), gauge_width),
    ]
    if active is None:
        return columns(cells, width, flexible=1)
    marker = text(ACTIVE_MARKER, ACTIVE_STYLE) if active else Content(" ")
    return columns([(marker, 1), *cells], width, flexible=2)


def session_contents(
    rows: Sequence[SessionRowParts],
    width: int | None,
    active: int | None = None,
    *,
    marks_active: bool = False,
) -> list[Content]:
    """Every row of one listing, with the state, age and gauge columns aligned across them.

    `marks_active` is what reserves the marker column, and `active` is the index inside it --
    `None` for a listing that marks the column but has nothing active right now, which is what
    a sessions position looks like between a vanished target and the owner choosing a new one.
    A caller that marks nothing (the dashboard's region) leaves both alone and draws the row it
    always drew.
    """
    if not rows:
        return []
    state_width = max(len(parts.state) for parts in rows)
    age_width = max(len(parts.age) for parts in rows)
    gauge_width = max(GAUGE_COLUMN, *(gauge_content(parts.gauge).cell_length for parts in rows))
    return [
        session_content(
            parts,
            width=width,
            state_width=state_width,
            age_width=age_width,
            gauge_width=gauge_width,
            active=(index == active) if marks_active else None,
        )
        for index, parts in enumerate(rows)
    ]


def session_counts_content(records: Sequence[SessionRecord]) -> Content:
    """`● 2 running · ● 1 starting · ● 2 attention · ○ 1 preserved`, empty buckets omitted.

    Counted from the same tuple the rows were drawn from (one read, never two). The glyph
    carries the group's colour; the count and word are plain, so the sentence survives
    `NO_COLOR` as `● 2 running` and still says which is which by the word.
    """
    pieces: list[Content] = []
    for group, count in group_counts(records).items():
        if not count:
            continue
        piece = Content.assemble(
            (GROUP_GLYPH[group], GROUP_STYLE[group]), (f" {count} {GROUP_STATUS_WORD[group]}", None)
        )
        pieces.append(piece)
    joined = Content("")
    for index, piece in enumerate(pieces):
        if index:
            joined = joined + Content(" · ")
        joined = joined + piece
    return joined


# --- projects -----------------------------------------------------------------------------


def project_row_content(name: str, last_used: datetime | None, width: int | None) -> Content:
    """`name` taking the slack and its last-launch age, muted, against the right edge; an em
    dash, dim, for a project never launched."""
    from remote_agents.application.relative_time import age

    used = text(age(last_used), MUTED) if last_used is not None else text(NO_GAUGE, DIM)
    return columns([(Content(name), None), (used, None)], width, flexible=0)


# --- plan limits --------------------------------------------------------------------------


def _percent_style(percent: int) -> str:
    if percent < 50:
        return "$success"
    if percent <= 85:
        return "$warning"
    return "$error"


_WINDOW_LABELS = {"week": "wk"}
"""The provider's `week` is this surface's `wk`: the pane is a third of a column wide."""

LIMIT_COLUMNS = ("5h", "week")
"""The window kinds every limits row draws, in this order, whatever any row published.

Fixed rather than collected from the readings (0.46.0). Collected first-seen, one row's state
reordered another's: Claude's row is read first, so when its five-hour window lapsed the
columns became `[week, 5h]` and Codex was drawn week-first. A kind outside this pair (Codex
can derive `day` from `window_minutes`) follows it, ordered by duration.
"""

_LABEL_MINUTES = {"m": 1, "h": 60, "d": 1440, "w": 10080}
_NAMED_MINUTES = {"day": 1440, "week": 10080}


def _label_minutes(label: str) -> float:
    """How long a window labelled `label` is, for ordering kinds outside `LIMIT_COLUMNS`."""
    if label in _NAMED_MINUTES:
        return _NAMED_MINUTES[label]
    match = re.fullmatch(r"(\d+)([mhdw])", label)
    if match is None:
        return math.inf
    return int(match.group(1)) * _LABEL_MINUTES[match.group(2)]


def _column_labels(seen: Iterable[str]) -> tuple[str, ...]:
    """`LIMIT_COLUMNS`, then any other published kind by duration -- never by first sight."""
    extra = {label for label in seen if label not in LIMIT_COLUMNS}
    return LIMIT_COLUMNS + tuple(sorted(extra, key=lambda label: (_label_minutes(label), label)))


#: How many cells `percent_gauge` draws, asked of it rather than written down again. The
#: number is `session_views._GAUGE_CELLS`' to own (DEC-043), and a second copy here would be a
#: lockstep site that nothing checks: the gauge would change width and this pane would keep
#: reserving the old one.
_GAUGE_WIDTH = len(percent_gauge(0))

#: From how wide a pane the week gauge widens, and to how many cells (DEC-106). A pace tick
#: at 8 cells moves in 12.5% steps -- nearly a day of a week -- so where there is room the week
#: bar doubles. Only the week: a 5h window has no pace, and a wider bar would say nothing more.
_WIDE_PANE = 70
_WIDE_WEEK_GAUGE = 16

#: The pace tick: where an even spend would stand today, drawn inside the bar it measures.
PACE_TICK = "┃"

#: The window whose pace the wide layout's two trailing columns carry. A `day` window keeps its
#: tick; the columns are the week's, which is the question the owner asks of the pane.
_PACE_WINDOW = "week"
_EXPECTED_HEADING = "expected"
_PACE_HEADING = "vs pace"


def pace_text(delta: int) -> str:
    """`on pace`, `▲ 14 over` or `▼ 7 under`: the surface's words for `pace_delta` (DEC-043).

    The arrow and the word carry the direction, so the colour `pace_style` adds is a second
    signal and never the only one (DEC-010).
    """
    if delta == 0:
        return "on pace"
    return f"▲ {delta} over" if delta > 0 else f"▼ {-delta} under"


def pace_style(delta: int) -> str:
    """On or under pace `$success`; over by up to 25 points `$warning`; further `$error`."""
    if delta <= 0:
        return "$success"
    if delta <= 25:
        return "$warning"
    return "$error"


def _week_pace(row: LimitRow):
    """The row's window that owns the pace columns, when it has pace; else None."""
    window = next((w for w in row.windows if w.label == _PACE_WINDOW), None)
    if window is None or window.expected_percent is None or window.pace_delta is None:
        return None
    return window


@dataclass(frozen=True, slots=True)
class _LimitColumns:
    """The column widths of one limits render, measured across every row in it.

    Measured across the **set**, never from the first row, for the reason `session_contents`
    already takes its maxima across the whole listing: the widths are a property of the table,
    and one derived from `rows[0]` is correct exactly until an agent drops out of the read and
    the order changes. `test_the_columns_are_a_property_of_the_set_not_of_the_first_row` is
    that failure written down.
    """

    profile: int
    label: int
    percent: int
    reset: int
    labels: tuple[str, ...] = LIMIT_COLUMNS
    week_gauge: int = _GAUGE_WIDTH
    """How many cells the week column's bar is drawn in: `_WIDE_WEEK_GAUGE` on a wide pane."""
    expected: int = len(_EXPECTED_HEADING)
    """The `expected` column: its heading, which is wider than any `100%`."""
    pace: int = len(_PACE_HEADING)
    """The `vs pace` column: the widest of its heading and every row's pace words."""
    """Which window kinds the table has a column for, left to right: `_column_labels`.

    A column is a *window kind*, not a position. Positional layout was BL-046: an agent that
    published only a weekly window had it drawn in the column its neighbour used for five
    hours, so the grid invited the owner to read one agent's week against another's afternoon,
    with both labels truthful. Every row draws every one of these columns.
    """


#: The narrowest the profile column is ever squeezed to before the name is ellipsised rather
#: than the data. Three cells is `a…`-plus-one: enough that the rows are still told apart,
#: little enough that the gauge survives a pane far narrower than any this app is drawn in.
#:
#: It exists because the profile column is a **max across every row**, so without a cap one
#: oddly-named profile degrades every row in the render: a 23-character
#: `claude-personal-max-20x` at a 40-cell pane produced 45-cell lines, and the widget's
#: `text-overflow: ellipsis` then ate the gauge outright, leaving a column of names with no
#: readable figure beside any of them. `columns()` has always had the equivalent guard one
#: function over -- it abandons padding below `room < 4` -- and this path had none.
_MINIMUM_PROFILE_COLUMN = 3

#: The gap between the profile and its first window, and between one window and the next. Two
#: rather than `_GUTTER`'s one, deliberately: it marks a group boundary against the single
#: spaces inside a window cell. Named because the stacked layout's indent has to agree with it,
#: and two literal `2`s that must match are two chances to change one of them.
_GROUP_GUTTER = 2


def _limit_columns(rows: Sequence[LimitRow], width: int | None = None) -> _LimitColumns:
    """The four column widths, measured across every row, with the profile capped to fit.

    The `default=0` fallbacks are not for an empty `rows` -- `limit_rows_content` returns early
    on that one level up -- but for a row that has *windows of its own* and none in the render
    at all, which `limit_rows` can still hand us.
    """
    windows = [(row, window) for row in rows for window in row.windows]
    labels = _column_labels(window.label for _row, window in windows)
    label = max(len(_WINDOW_LABELS.get(kind, kind)) for kind in labels)
    percent = max((len(f"{window.percent}%") for _row, window in windows), default=0)
    reset = max((len(_reset_text(row, window)) for row, window in windows), default=0)
    profile = max((len(row.profile) for row in rows), default=0)
    # The absence phrase is deliberately *not* measured into any column width. It trails the
    # row's last column, so nothing is drawn after it and it aligns nothing.
    paces = [_week_pace(row) for row in rows]
    pace = max(
        (len(pace_text(window.pace_delta)) for window in paces if window is not None),
        default=0,
    )
    return _LimitColumns(
        profile=_capped_profile(profile, width, label=label, percent=percent, reset=reset),
        label=label,
        percent=percent,
        reset=reset,
        labels=labels,
        week_gauge=_WIDE_WEEK_GAUGE if width is not None and width >= _WIDE_PANE else _GAUGE_WIDTH,
        pace=max(len(_PACE_HEADING), pace),
    )


def _gauge_cells(label: str, columns: _LimitColumns) -> int:
    return columns.week_gauge if label == "week" else _GAUGE_WIDTH


def limit_gauge_content(percent: int, cells: int, expected: int | None) -> Content:
    """A window's bar, with the pace tick in it when `expected` is given.

    The tick sits at `round(expected/100 * cells)`, clamped to the last cell so a spent window
    still draws it inside. It replaces the cell it lands on rather than widening the bar, and it
    is `$text`; the fill keeps its threshold colour on both sides of it and the track stays
    `$secondary`, so the glyph alone marks where an even spend would be (DEC-010).
    """
    bar = percent_gauge(percent, cells)
    filled = len(bar.rstrip("░"))
    fill = _percent_style(percent)
    if expected is None:
        return Content.assemble((bar[:filled], fill), (bar[filled:], "$secondary"))
    tick = min(cells - 1, max(0, round(expected / 100 * cells)))
    parts = []
    for start, end in ((0, min(tick, filled)), (min(tick, filled), tick)):
        if end > start:
            parts.append((bar[start:end], fill if start < filled else "$secondary"))
    parts.append((PACE_TICK, "$text"))
    rest = bar[tick + 1 :]
    after = max(0, filled - tick - 1)
    parts.append((rest[:after], fill))
    parts.append((rest[after:], "$secondary"))
    return Content.assemble(*(part for part in parts if part[0]))


def _capped_profile(
    profile: int, width: int | None, *, label: int, percent: int, reset: int
) -> int:
    """Shrink the profile column until one whole window fits beside it, never below the floor.

    The window is the row's payload and the name is its handle, so when the two cannot both
    have what they want it is the name that gives way -- the same order of preference
    `activity_text` applies when a session's display name and an agent's words compete for a
    message budget.
    """
    if width is None or width <= 0:
        return profile
    window = label + 1 + _GAUGE_WIDTH + 1 + percent + (1 + reset if reset else 0)
    room = width - _GROUP_GUTTER - window
    return max(_MINIMUM_PROFILE_COLUMN, min(profile, room))


def _window_label(window) -> str:
    return _WINDOW_LABELS.get(window.label, window.label)


def _reset_text(row: LimitRow, window) -> str:
    """`↻ 2h`, or nothing when the provider published no reset or the reading is stale.

    A countdown on a stale number is a claim about the present made from the past, which is why
    the trailer replaces every countdown with one dim `· as of 2h` instead.
    """
    if window.resets_in is None or row.stale_for is not None:
        return ""
    return f"↻ {window.resets_in}"


def _window_content(
    row: LimitRow, window, columns: _LimitColumns, *, last: bool, labelled: bool = True
) -> Content:
    """`5h ███░░░░░  34% ↻ 2h` -- one window's cell, laid out to the table's columns.

    Dated instead of counted down when stale. The space after the arrow is deliberate:
    terminal fonts draw `↻` wider than one cell and it overlapped the digit that followed.

    **Three fields are padded and each is padded the way its content is read.** The label is
    left-aligned, because `5h`, `wk` and `day` are names; the percent is **right**-aligned,
    because `3%`, `34%` and `100%` are figures and a column of figures is compared down its
    right edge; the reset keeps its own width so the window that follows starts where the one
    above it did. Without the percent padding the gauges could align and the cell after them
    still be pushed apart, which is the defect one column over rather than the defect fixed.

    `last` suppresses the reset field's trailing pad on the final window of a row. Nothing
    follows it, so the padding buys no alignment — and it would count toward the row's
    `cell_length`, tipping a row that fits into the narrow branch on the strength of spaces
    the owner cannot see.

    `labelled=False` is the one-line layout's cell: its header row names the column once, so
    the cell starts at its bar.
    """
    label = [(_window_label(window).ljust(columns.label), MUTED), (" ", None)] if labelled else []
    cell = Content.assemble(
        *label,
        limit_gauge_content(
            window.percent, _gauge_cells(window.label, columns), window.expected_percent
        ),
        (f" {f'{window.percent}%'.rjust(columns.percent)}", None),
    )
    if not columns.reset:
        return cell
    reset = _reset_text(row, window)
    padded = reset if last else reset.ljust(columns.reset)
    if not padded:
        return cell
    return cell + Content.assemble((f" {padded}", MUTED))


def _empty_window_content(
    label: str, columns: _LimitColumns, *, last: bool, labelled: bool = True
) -> Content:
    """A window this row did not publish: its label and an empty bar, the figures left blank.

    Blank at their widths rather than dropped, so the next column starts where it does on
    every other row. DEC-010: the missing figure is told by the absent percent, not a colour.
    """
    named = [(_WINDOW_LABELS.get(label, label).ljust(columns.label), MUTED), (" ", None)]
    cell = Content.assemble(
        *(named if labelled else []),
        (percent_gauge(0, _gauge_cells(label, columns)), "$secondary"),
        (" " * (1 + columns.percent), None),
    )
    if columns.reset and not last:
        cell = cell + Content(" " * (1 + columns.reset))
    return cell


def _row_windows(row: LimitRow) -> dict[str, object]:
    """This row's windows, keyed by the column each one belongs in."""
    return {window.label: window for window in row.windows}


def _absence_cell(row: LimitRow, columns: _LimitColumns) -> Content:
    """A row's silence, after its bars (`_note`), muted and in words.

    Words rather than a colour or a dash, per DEC-010: the pane is read in monochrome by
    someone who has never been told a convention, and a dash would be a fourth thing meaning
    none of DEC-061's three. Not padded to a cell width -- nothing follows it, and padding
    would only inflate the row's measured length.
    """
    return Content.assemble((row.absence or "", MUTED))


def _name(row: LimitRow, columns: _LimitColumns) -> Content:
    """The profile, padded to the column -- or ellipsised into it when the name is the thing
    that does not fit. `truncate` both pads and cuts, which is what `columns()` uses one
    function over for exactly this."""
    return text(row.profile, None).truncate(columns.profile, ellipsis=True, pad=True)


def _one_line(row: LimitRow, columns: _LimitColumns) -> Content:
    """The row, laid out against the table's columns rather than against its own windows.

    Walking `columns.labels` rather than `row.windows` is the whole of BL-046's fix: a window
    is drawn in the column its *kind* owns. Every row walks every column (0.46.0): a kind this
    row did not publish is drawn as its label and an empty bar. What the row says after its
    bars is `_note`'s, added by `limit_row_content`.
    """
    line = _name(row, columns)
    published = _row_windows(row)
    for label in columns.labels:
        window = published.get(label)
        if window is None:
            cell = _empty_window_content(label, columns, last=False, labelled=False)
        else:
            cell = _window_content(row, window, columns, last=False, labelled=False)
        line = line + Content(" " * _GROUP_GUTTER) + cell
    # The pace columns (DEC-106): blank at their widths for a row with no week pace.
    paced = _week_pace(row)
    gutter = " " * _GROUP_GUTTER
    if paced is not None:
        line = line + Content.assemble(
            (gutter, None),
            (f"{paced.expected_percent}%".rjust(columns.expected), MUTED),
            (gutter, None),
            (pace_text(paced.pace_delta), pace_style(paced.pace_delta)),
        )
    # Trailing blanks align nothing, and they would count toward the length that decides
    # whether the note fits beside the bars.
    return line.rstrip()


def _cell_width(label: str, columns: _LimitColumns) -> int:
    """One window's cell in the one-line layout, unlabelled: bar, percent and reset."""
    cell = _gauge_cells(label, columns) + 1 + columns.percent
    if columns.reset:
        cell += 1 + columns.reset
    return cell


def _header_line(columns: _LimitColumns) -> Content:
    """The one-line layout's column names: `5h`, `week`, `expected` and `vs pace`, muted.

    Named once over the table rather than in every row: two agents' rows repeating `5h` and
    `wk` said the same thing twice, and the two new columns need names somewhere.
    """
    parts = [" " * columns.profile]
    for label in columns.labels:
        parts.append(" " * _GROUP_GUTTER + label.ljust(_cell_width(label, columns)))
    parts.append(" " * _GROUP_GUTTER + _EXPECTED_HEADING.rjust(columns.expected))
    parts.append(" " * _GROUP_GUTTER + _PACE_HEADING)
    return Content.assemble(("".join(parts), MUTED))


def _table_width(columns: _LimitColumns) -> int:
    """How wide one row's bars can be: the profile and every column, at their widths.

    What the stack decision is made from, and nothing else (0.46.0). Every row draws every
    column, so this is a property of the table; what a row says *after* its bars -- a stale
    date, an absence phrase -- takes a line of its own when it does not fit, rather than
    flipping the whole pane between layouts as a reading ages.
    """
    windows = sum(_GROUP_GUTTER + _cell_width(label, columns) for label in columns.labels)
    pace = _GROUP_GUTTER + columns.expected + _GROUP_GUTTER + columns.pace
    return columns.profile + windows + pace


def _note(row: LimitRow, columns: _LimitColumns) -> tuple[Content, Content]:
    """What a row says after its bars, as (separator, words): its silence, or its age.

    DEC-061's absences trail the bars rather than replacing them, so a row with no reading
    still draws every column. The borrowed-source stamp DEC-061 asks for is not drawn here --
    it cost the console's 73-column pane its one-line row (removed 2026-09-03 on the owner's
    ask); the bot still says it.
    """
    # A row saying *no reading yet* must not also say *· as of 3d*: the date is a reading's,
    # so beside a phrase denying there is one it contradicts the row it trails. Reachable --
    # Codex stamps `observed_at` from the rollout record even when every window in it has
    # lapsed, which is the ordinary idle host.
    if row.absence:
        return Content(" " * _GROUP_GUTTER), _absence_cell(row, columns)
    if row.stale_for is not None:
        return Content.assemble((" · ", DIM)), Content.assemble((f"as of {row.stale_for}", DIM))
    return Content(""), Content("")


def limit_row_content(
    row: LimitRow, columns: _LimitColumns, width: int | None, *, stack: bool | None = None
) -> list[Content]:
    """`profile  5h ███░░░░░  34% ↻ 2h  wk █████░░░  61% ↻ 3d`, or one line per window when narrow.

    The gauge's fill takes the threshold colour -- under half `$success`, up to 85 `$warning`,
    past it `$error` -- and its empty track `$secondary`. The reset countdown is muted; a
    reading older than the staleness bound replaces every countdown with one dim `· as of 2h`,
    because a countdown on a stale number is a claim about the present made from the past. A
    borrowed figure names its source, dim, as DEC-061 requires of presentation.

    One grid row where the pane is wide enough for it; where it is not -- the dashboard's right
    column at 100 columns is 38 cells, and two windows with countdowns run to 46 -- each window
    takes a line of its own under the profile, and the row's note (`_note`) a line of its own
    when it does not fit beside the last bar, so a gauge is never broken across two rows and a
    stale date or an absence phrase is never the part that falls off. The pane draws these
    `nowrap`, so what this returns *is* the rows.

    **Every field is padded to a width measured across the whole render** (`columns`), so the
    Nth window of every agent begins in the same column and the pane reads as one table rather
    than as one sentence per agent. That is what the sessions pane has always done through
    `session_contents`, and this list is the one in the surface that did not.
    """
    if stack is None:
        stack = width is not None and width > 0 and _table_width(columns) > width
    published = _row_windows(row)
    indent = Content(" " * (columns.profile + _GROUP_GUTTER))
    if stack:
        # Every column, one per line, in `columns.labels` order -- a window this row did not
        # publish is its label and an empty bar here too. Each cell is built unpadded: the next
        # window is on the next line, so a reset pad would align nothing and only inflate the
        # length that decides whether the note fits beside the last one.
        cells = [
            (
                _empty_window_content(label, columns, last=True)
                if published.get(label) is None
                else _window_content(row, published[label], columns, last=True)
            ).rstrip()
            for label in columns.labels
        ]
        lines = [_name(row, columns) + Content(" " * _GROUP_GUTTER) + cells[0]]
        lines.extend(indent + cell for cell in cells[1:])
    else:
        lines = [_one_line(row, columns)]
    separator, words = _note(row, columns)
    if words:
        if width is None or width <= 0 or (lines[-1] + separator + words).cell_length <= width:
            lines[-1] = lines[-1] + separator + words
        else:
            lines.append(indent + words)
    return lines


def limit_rows_content(rows: Sequence[LimitRow], width: int | None = None) -> list[Content]:
    """Every agent's windows, with the label, gauge, percent and reset columns aligned across
    them — the limits pane's answer to `session_contents`."""
    if not rows:
        return []
    columns = _limit_columns(rows, width)
    # **Decided once, for the whole render.** Asking each row whether it fits produced a band
    # of widths -- seven columns wide, between a pane that stacks everything and one that
    # stacks nothing -- where a short row stayed on one line while a longer one stacked, so
    # one agent's second window sat at column 30 and another's at column 8. That is this
    # stage's own goal failing inside a single render, and the reason is that the layout is a
    # property of the table rather than of the row. The routine case is exactly the one that
    # hits it: Claude's borrowed reading goes stale behind a thirty-minute fence while Codex's
    # does not, so one row carries countdowns and the other carries a date. Since 0.46.0 it is
    # made from the column set alone (`_table_width`), so no row's data can flip it.
    stack = width is not None and width > 0 and _table_width(columns) > width
    lines = [line for row in rows for line in limit_row_content(row, columns, width, stack=stack)]
    return lines if stack else [_header_line(columns), *lines]


# --- feed ---------------------------------------------------------------------------------


def feed_row_content(
    kind: ActivityKind,
    kind_word: str,
    identity: str,
    sequence: int | None,
    detail: str | None,
    observed_at: datetime,
    age_text: str,
    *,
    width: int | None,
    kind_width: int,
    age_width: int,
    ask_words: str | None = None,
) -> Content:
    """`glyph kind  identity #n — detail  age`, the identity and detail taking the slack.

    A row older than `FEED_HISTORY_AGE` is muted whole. The detail is an agent's own words and
    arrives as literal text: `Content(detail)`, never markup.

    **`ask_words` is not a detail and is not drawn like one.** It is this surface's phrase for
    the class of thing an agent is waiting on -- words this service chose, not words an agent
    wrote -- so it is parenthesised and dimmer, where a detail follows an em dash at the row's
    ordinary muted weight. They shared one slot and one style until the Stage 3 gate evaluator
    pointed out that the bot keeps the two kinds of string structurally apart and the pane did
    not, which is DEC-067's conflation argument reappearing at a presentation slot rather than
    at a port field.

    **A row carries either, never both** -- an agent that said something is quoted, and the
    class is what there is to say when it did not.

    **That rule went and came back, and the round trip is the useful part.** DEC-098 made a
    Codex ask carry the real command, and on 2026-09-19 this drew both on the reading that the
    class says what KIND of answer is wanted while the detail says what it is about. Shown the
    result, the owner called it redundant, and they are right: with the command on the row, the
    line read `needs answer … (about a shell command) — $ rm -rf build/` -- the same fact three
    times, in a cell that has to share its width with the session identity.

    The class earns its place only when nothing else says what the wait is about, which is
    exactly the `elif` below. The Telegram headline is unaffected and still carries the clause,
    because there it is a sentence with a collapsed quotation underneath rather than a
    parenthetical competing for one line.
    """
    history = datetime.now(UTC) - observed_at > FEED_HISTORY_AGE
    kind_style = MUTED if history else KIND_STYLE[kind]
    body_style = MUTED if history else None
    body = Content.assemble((identity, body_style))
    if sequence is not None:
        body = body + Content.assemble((f" #{sequence}", MUTED))
    if detail:
        body = body + Content.assemble((" — ", MUTED), (detail, MUTED))
    elif ask_words:
        body = body + Content.assemble((f" ({ask_words})", body_style or DIM))
    cells: list[tuple[Content, int | None]] = [
        (text(KIND_GLYPH[kind], kind_style), 1),
        (text(kind_word, kind_style), kind_width),
        (body, None),
        # Right-aligned: the column is wider than most ages, and an age is read at the edge.
        (text(age_text.rjust(age_width), MUTED), age_width),
    ]
    if width is not None and width - (1 + kind_width + age_width + 3) < FEED_NARROW_ROOM:
        # The glyph alone carries the kind where the word would take the identity's room --
        # the dashboard's feed region is a third of a column, and the row exists to say which
        # session an event belongs to before it says anything else.
        del cells[1]
    return columns(cells, width, flexible=len(cells) - 2)
