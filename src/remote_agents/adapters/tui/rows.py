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


def _window_content(row: LimitRow, window) -> Content:
    """`5h ███░░░░░ 34% ↻ 2h` -- one window's cell, dated instead of counted down when stale.
    The space after the arrow is deliberate: terminal fonts draw `↻` wider than one cell and it
    overlapped the digit that followed."""
    bar = percent_gauge(window.percent)
    filled = bar.rstrip("░")
    cell = Content.assemble(
        (_WINDOW_LABELS.get(window.label, window.label), MUTED),
        (" ", None),
        (filled, _percent_style(window.percent)),
        (bar[len(filled) :], "$secondary"),
        (f" {window.percent}%", None),
    )
    if window.resets_in is not None and row.stale_for is None:
        cell = cell + Content.assemble((f" ↻ {window.resets_in}", MUTED))
    return cell


def _trailers(row: LimitRow) -> Content:
    """What a row says after its windows: how old a stale reading is, dim. The borrowed-source
    stamp DEC-061 asks for is not drawn here -- it cost the console's 73-column pane its
    one-line row (removed 2026-09-03 on the owner's ask); the bot still says it."""
    trailer = Content("")
    if row.stale_for is not None:
        trailer = trailer + Content.assemble((f" · as of {row.stale_for}", DIM))
    return trailer


def limit_row_content(row: LimitRow, profile_width: int, width: int | None) -> list[Content]:
    """`profile  5h ███░░░░░ 34% ↻ 2h  wk █████░░░ 61% ↻ 3d`, or one line per window when narrow.

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
    """
    name = text(row.profile.ljust(profile_width), None)
    cells = [_window_content(row, window) for window in row.windows]
    trailer = _trailers(row)
    one_line = name
    for cell in cells:
        one_line = one_line + Content("  ") + cell
    one_line = one_line + trailer
    if width is None or width <= 0 or one_line.cell_length <= width:
        return [one_line]
    indent = Content(" " * (profile_width + 2))
    lines = [name + Content("  ") + cells[0]] if cells else [name]
    lines.extend(indent + cell for cell in cells[1:])
    if trailer:
        if (lines[-1] + trailer).cell_length <= width:
            lines[-1] = lines[-1] + trailer
        else:
            lines.append(indent + Content(trailer.plain.lstrip(" ·")).stylize(DIM))
    return lines


def limit_rows_content(rows: Sequence[LimitRow], width: int | None = None) -> list[Content]:
    if not rows:
        return []
    profile_width = max(len(row.profile) for row in rows)
    return [line for row in rows for line in limit_row_content(row, profile_width, width)]


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
) -> Content:
    """`glyph kind  identity #n — detail  age`, the identity and detail taking the slack.

    A row older than `FEED_HISTORY_AGE` is muted whole. The detail is an agent's own words and
    arrives as literal text: `Content(detail)`, never markup.
    """
    history = datetime.now(UTC) - observed_at > FEED_HISTORY_AGE
    kind_style = MUTED if history else KIND_STYLE[kind]
    body_style = MUTED if history else None
    body = Content.assemble((identity, body_style))
    if sequence is not None:
        body = body + Content.assemble((f" #{sequence}", MUTED))
    if detail:
        body = body + Content.assemble((" — ", MUTED), (detail, MUTED))
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
