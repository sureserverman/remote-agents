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

from collections.abc import Sequence
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
) -> Content:
    """One session row: `glyph identity #n  state  age  gauge`, the identity taking the slack.

    A preserved row's identity is muted whole, not just its glyph -- the row is there to be
    found, not to be scanned past what is live. The sequence is always muted: it is the handle
    the row keys act on, and the name is what the eye reads first.
    """
    identity_style = MUTED if parts.group is StateGroup.PRESERVED else None
    identity = Content.assemble((parts.identity, identity_style), (f" #{parts.sequence}", MUTED))
    if parts.note:
        identity = identity + Content.assemble((f" · {parts.note}", MUTED))
    return columns(
        [
            (session_glyph(parts.group), 1),
            (identity, None),
            (text(parts.state, GROUP_STYLE[parts.group]), state_width),
            (text(parts.age, MUTED), age_width),
            (gauge_content(parts.gauge), gauge_width),
        ],
        width,
        flexible=1,
    )


def session_contents(rows: Sequence[SessionRowParts], width: int | None) -> list[Content]:
    """Every row of one listing, with the state, age and gauge columns aligned across them."""
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
        )
        for parts in rows
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

#: How many cells `percent_gauge` draws, asked of it rather than written down again. The
#: number is `session_views._GAUGE_CELLS`' to own (DEC-043), and a second copy here would be a
#: lockstep site that nothing checks: the gauge would change width and this pane would keep
#: reserving the old one.
_GAUGE_WIDTH = len(percent_gauge(0))


def _limit_columns(rows: Sequence[LimitRow], width: int | None = None) -> _LimitColumns:
    """The four column widths, measured across every row, with the profile capped to fit.

    The `default=0` fallbacks are not for an empty `rows` -- `limit_rows_content` returns early
    on that one level up -- but for a row that has *windows of its own* and none in the render
    at all, which `limit_rows` can still hand us.
    """
    windows = [(row, window) for row in rows for window in row.windows]
    label = max((len(_window_label(window)) for _row, window in windows), default=0)
    percent = max((len(f"{window.percent}%") for _row, window in windows), default=0)
    reset = max((len(_reset_text(row, window)) for row, window in windows), default=0)
    profile = max((len(row.profile) for row in rows), default=0)
    return _LimitColumns(
        profile=_capped_profile(profile, width, label=label, percent=percent, reset=reset),
        label=label,
        percent=percent,
        reset=reset,
    )


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


def _window_content(row: LimitRow, window, columns: _LimitColumns, *, last: bool) -> Content:
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
    """
    bar = percent_gauge(window.percent)
    filled = bar.rstrip("░")
    cell = Content.assemble(
        (_window_label(window).ljust(columns.label), MUTED),
        (" ", None),
        (filled, _percent_style(window.percent)),
        (bar[len(filled) :], "$secondary"),
        (f" {f'{window.percent}%'.rjust(columns.percent)}", None),
    )
    if not columns.reset:
        return cell
    reset = _reset_text(row, window)
    padded = reset if last else reset.ljust(columns.reset)
    if not padded:
        return cell
    return cell + Content.assemble((f" {padded}", MUTED))


def _trailers(row: LimitRow) -> Content:
    """What a row says after its windows: how old a stale reading is, dim. The borrowed-source
    stamp DEC-061 asks for is not drawn here -- it cost the console's 73-column pane its
    one-line row (removed 2026-09-03 on the owner's ask); the bot still says it."""
    trailer = Content("")
    if row.stale_for is not None:
        trailer = trailer + Content.assemble((f" · as of {row.stale_for}", DIM))
    return trailer


def _name(row: LimitRow, columns: _LimitColumns) -> Content:
    """The profile, padded to the column -- or ellipsised into it when the name is the thing
    that does not fit. `truncate` both pads and cuts, which is what `columns()` uses one
    function over for exactly this."""
    return text(row.profile, None).truncate(columns.profile, ellipsis=True, pad=True)


def _one_line(row: LimitRow, columns: _LimitColumns, trailer: Content) -> Content:
    line = _name(row, columns)
    for index, window in enumerate(row.windows):
        cell = _window_content(row, window, columns, last=index == len(row.windows) - 1)
        line = line + Content(" " * _GROUP_GUTTER) + cell
    return line + trailer


def _overflows(row: LimitRow, columns: _LimitColumns, width: int | None) -> bool:
    if width is None or width <= 0:
        return False
    return _one_line(row, columns, _trailers(row)).cell_length > width


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
    takes a line of its own under the profile, and the trailers a line of their own after
    those, so a gauge is never broken across two rows and the borrowed-source stamp is never
    the part that falls off. The pane draws these `nowrap`, so what this returns *is* the rows.

    **Every field is padded to a width measured across the whole render** (`columns`), so the
    Nth window of every agent begins in the same column and the pane reads as one table rather
    than as one sentence per agent. That is what the sessions pane has always done through
    `session_contents`, and this list is the one in the surface that did not.
    """
    trailer = _trailers(row)
    if stack is None:
        stack = _overflows(row, columns, width)
    if not stack:
        return [_one_line(row, columns, trailer)]
    # Rebuilt without the reset field's trailing pad. That pad exists so the window *after*
    # this one starts in a fixed column; here the next window is on the next line, so it
    # aligns nothing and only inflates `cell_length` -- the measurement that decides whether
    # the trailer fits beside the last window, which would then be pushed onto a line of its
    # own by spaces the owner cannot see. **The trailer-fit arithmetic below depends on this**,
    # which is the coupling that produced the defect this suppression fixed.
    name = _name(row, columns)
    stacked = [_window_content(row, window, columns, last=True) for window in row.windows]
    indent = Content(" " * (columns.profile + _GROUP_GUTTER))
    lines = [name + Content(" " * _GROUP_GUTTER) + stacked[0]] if stacked else [name]
    lines.extend(indent + cell for cell in stacked[1:])
    if trailer:
        if width is None or width <= 0 or (lines[-1] + trailer).cell_length <= width:
            lines[-1] = lines[-1] + trailer
        else:
            lines.append(indent + Content(trailer.plain.lstrip(" ·")).stylize(DIM))
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
    # does not, so one row carries countdowns and the other carries a date.
    stack = any(_overflows(row, columns, width) for row in rows)
    return [line for row in rows for line in limit_row_content(row, columns, width, stack=stack)]


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

    A row may carry either, never both: an agent that said something is quoted, and the class
    is what there is to say when it did not.
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
        (text(age_text, MUTED), age_width),
    ]
    if width is not None and width - (1 + kind_width + age_width + 3) < FEED_NARROW_ROOM:
        # The glyph alone carries the kind where the word would take the identity's room --
        # the dashboard's feed region is a third of a column, and the row exists to say which
        # session an event belongs to before it says anything else.
        del cells[1]
    return columns(cells, width, flexible=len(cells) - 2)
