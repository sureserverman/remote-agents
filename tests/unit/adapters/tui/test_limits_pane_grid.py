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
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from backends import SessionUseCaseDouble, backend_for
from textual.widgets import OptionList

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.rows import (
    limit_gauge_content,
    limit_rows_content,
    pace_style,
    pace_text,
)
from remote_agents.adapters.tui.screens.dashboard import (
    _CLAUDE_REMOTE_CONTROL_ROW,
    _EMPTY_LIMITS_ROW,
    _HOST_REMOTE_CONTROL_ROW,
    DashboardScreen,
    LimitsPaneScreen,
)
from remote_agents.application.host_remote_control import HOST_REMOTE_CONTROL_TITLE
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.remote_control_default import (
    REMOTE_CONTROL_DEFAULT_TITLE,
    UNAVAILABLE,
    remote_control_default_line,
)
from remote_agents.application.session_views import LimitRow, LimitWindow, limit_rows
from remote_agents.domain.models import ProfileId, SessionRecord
from remote_agents.domain.remote_control import RemoteControlDefault
from remote_agents.ports.agent_usage import AgentLimits, UsageWindow

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
    spans = _gauge_spans(line)
    for index, (_start, gauge_end) in enumerate(spans):
        # Only up to the next gauge: an unpublished window's bar has no percent after it, and
        # searching past it would find the next window's.
        stop = spans[index + 1][0] if index + 1 < len(spans) else len(line)
        marker = line.find("%", gauge_end, stop)
        if marker >= 0:
            ends.append(marker + 1)
    return ends


def _grid(rows, width: int = WIDE) -> tuple[str, list[str]]:
    """The one-line layout as (header, row lines).

    Since DEC-106 the column names are a header row over the table rather than a label in
    every cell, so a column's label is read off the header, not off the row.
    """
    header, *lines = [content.plain for content in limit_rows_content(rows, width)]
    assert header.split()[0] == "5h", f"the first line is not the header: {header!r}"
    return header, lines


def _labelled_gauges(header: str, line: str) -> dict[str, tuple[int, int]]:
    """Each drawn window's gauge span, keyed by the label naming the column it sits in.

    Keyed by label rather than by position because that is what a column now *is* (BL-046).
    The label is the header word that begins where the gauge does -- the header names each
    column over its bar -- so these assertions follow the layout instead of restating its
    arithmetic. A gauge under no header word has no column, and is reported as `?`.
    """
    found = {}
    for start, end in _gauge_spans(line):
        word = header[start:].split()[0] if header[start : start + 1].strip() else "?"
        found[word] = (start, end)
    return found


def _column_offsets(header: str, lines: list[str]) -> dict[str, set[int]]:
    """Every offset each window kind was drawn at, across the whole render.

    A grid is exactly the claim that each of these sets has one member.
    """
    offsets: dict[str, set[int]] = {}
    for line in lines:
        for label, (start, _end) in _labelled_gauges(header, line).items():
            offsets.setdefault(label, set()).add(start)
    return offsets


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


def test_a_window_kind_begins_at_one_offset_across_the_whole_render() -> None:
    """A column belongs to a window *kind*, and a kind is drawn at one offset (BL-046).

    This used to read "the gauge of window N begins at one offset", which was the same claim
    only for as long as every agent published the same kinds in the same order. The moment one
    did not -- the routine case, since Codex publishes whatever its rollout carried -- the
    positional version was satisfied by drawing a weekly window under a five-hour one.
    """
    header, lines = _grid(_rows())
    assert len(lines) == len(_rows()), "each row should occupy exactly one line at this width"

    offsets = _column_offsets(header, lines)
    # Read back as drawn: the header names the week column `week` (the stacked layout's cells
    # abbreviate it to `wk`), and these assertions are about what the owner sees.
    assert set(offsets) == {"5h", "week", "day"}, offsets
    for label, starts in offsets.items():
        assert len(starts) == 1, (
            f"{label} is drawn at {sorted(starts)}; a column is one offset.\n" + "\n".join(lines)
        )

    # And the columns are laid out in one fixed order, left to right -- `5h`, then `wk`, then
    # any other kind by duration -- so a row's windows cannot be permuted into somebody else's
    # columns while still "aligning".
    ordered = [label for label, _ in sorted(offsets.items(), key=lambda pair: min(pair[1]))]
    assert ordered == ["5h", "week", "day"], ordered


def test_a_window_kind_ends_its_percent_at_one_offset() -> None:
    """A percent is read by comparing figures, so a kind's figures share a right edge.

    Separate from the gauge assertion because the two fail for different repairs: aligning the
    starts alone still lets `3%` and `100%` push the following cell apart, which is the defect
    one column over rather than the defect fixed.
    """
    header, lines = _grid(_rows())
    ends: dict[str, set[int]] = {}
    for line in lines:
        for label, (_start, gauge_end) in _labelled_gauges(header, line).items():
            marker = line.find("%", gauge_end)
            if line[_start:gauge_end].strip("░"):
                ends.setdefault(label, set()).add(marker + 1)

    for label, edges in ends.items():
        assert len(edges) == 1, (
            f"{label}'s percent ends at {sorted(edges)} across rows.\n" + "\n".join(lines)
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
    header, lines = _grid(reordered)

    for label, starts in _column_offsets(header, lines).items():
        assert len(starts) == 1, (
            f"{label} is drawn at {sorted(starts)} for {profiles}.\n" + "\n".join(lines)
        )


# --- gate remediation, 2026-09-06 -----------------------------------------------------------
#
# Three cases the first round did not cover, two of them Material findings from the stage
# gate's evaluator and one a Tier-2 suggestion. Each is a shape real data takes and the
# original fixture did not.


def test_rows_with_different_window_counts_still_agree_on_window_zero() -> None:
    """One agent publishing one window beside another publishing three.

    Since 0.46.0 every row draws every column, so the one-window row carries two empty ones;
    what this still guards is the `last=` padding, which must not move window 0 on either row.
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
    _header, lines = _grid(rows)
    per_row = [_gauge_spans(line) for line in lines]
    assert [len(spans) for spans in per_row] == [3, 3], lines

    assert len({spans[0][0] for spans in per_row}) == 1, "window 0 misaligned:\n" + "\n".join(lines)
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


# --- columns are a window kind, not a position (BL-046) ------------------------------------


def _asymmetric() -> tuple[LimitRow, ...]:
    """One agent publishing two windows, one publishing only the second of them.

    BL-046's own shape, and not a contrived one: Claude publishes a five-hour and a weekly
    window, Codex publishes whatever its rollout carried, and a host whose Codex has only
    crossed into its weekly window publishes exactly this. Positional layout puts `wk` under
    `5h` here -- two different questions in one column, both labelled truthfully, which is
    what makes the misreading so easy.
    """
    return (
        LimitRow(
            "claude", (LimitWindow("5h", 34, "2h"), LimitWindow("week", 61, "3d")), None, None
        ),
        LimitRow("codex", (LimitWindow("week", 9, None),), None, None),
    )


def test_a_window_lands_under_the_same_window_and_not_under_the_same_position() -> None:
    """BL-046: `wk` sits under `wk`, and the column `5h` owns stays `5h`'s.

    Asserted on the label's own offset rather than the gauge's, because the label is what
    names the column; a gauge that happened to line up while the labels did not would be the
    same defect drawn more carefully.
    """
    header, lines = _grid(_asymmetric())
    assert len(lines) == 2, "\n".join(lines)
    claude, codex = (_labelled_gauges(header, line) for line in lines)

    assert claude["week"] == codex["week"], (
        "the weekly window sits in two different columns, so the grid reads one agent's week "
        "against another's five hours.\n" + "\n".join((header, *lines))
    )
    # Codex's only window is its week: the bar under `week` carries a figure, and the bar under
    # `5h` is empty -- its week has not been pulled into the column 5h owns.
    week_start, week_end = codex["week"]
    five_start, five_end = codex["5h"]
    assert set(lines[1][five_start:five_end]) == {"░"}, "\n".join((header, *lines))
    assert "█" in lines[1][week_start:week_end], "\n".join((header, *lines))


def test_a_column_a_row_does_not_publish_keeps_its_label_and_an_empty_bar() -> None:
    """A row keeps the shape of the table it belongs to: label and empty bar, no figure.

    Until 0.46.0 the unpublished column was blank. The owner asked for every element always
    present, so the label stays and the bar is drawn empty; the missing figure is told by the
    absent percent (DEC-010: no colour carries it).
    """
    header, lines = _grid(_asymmetric())
    _claude, codex = lines

    # The span the five-hour column occupies: from where the header names it to where it names
    # the next one. Read off the header rather than written down, so the assertion follows the
    # layout instead of restating it. The label is the header's since DEC-106.
    cell = slice(header.index("5h"), header.index("week"))
    drawn = codex[cell]
    assert drawn.split()[:1] == ["░" * 8], (
        f"codex's five-hour column should be an empty bar under its label -- found {drawn!r}.\n"
        + "\n".join((header, *lines))
    )
    figures = drawn[drawn.index("░") :]
    assert not re.search(r"[0-9%]", figures), f"an unpublished window shows a figure: {drawn!r}"


def test_a_row_with_no_windows_draws_empty_bars_then_says_which_silence_it_is() -> None:
    """DEC-061's three absences, drawn as words after the row's empty bars.

    A word rather than a colour or a dash (DEC-010): the grid has to survive monochrome, and a
    dash would be a fourth thing meaning none of the three. Until 0.46.0 the phrase took the
    bars' place; it now trails them, so every row has every column and the reason still reads.
    """
    rows = (
        LimitRow("claude", (LimitWindow("5h", 34, "2h"),), None, None),
        LimitRow("cursor-agent", (), None, None, absence="never reported"),
        LimitRow("opencode", (), None, None, absence="no reading yet"),
        LimitRow("codex", (), None, None, absence="unreadable"),
    )
    _header, lines = _grid(rows)
    assert len(lines) == 4, "\n".join(lines)

    for line, phrase in zip(lines[1:], ("never reported", "no reading yet", "unreadable")):
        bars = _gauge_spans(line)
        # Empty, whatever their width: the week bar widens on a wide pane (DEC-106).
        assert len(bars) == 2 and all(set(line[a:b]) == {"░"} for a, b in bars), line
        assert line.index(phrase) > bars[-1][1], f"{phrase!r} should trail the bars.\n" + "\n".join(
            lines
        )

    assert len({line.split()[0] for line in lines}) == 4, "every agent keeps its own row"


def test_a_row_that_says_it_has_no_reading_does_not_also_date_one() -> None:
    """`no reading yet · as of 3d` is a row contradicting itself, and it was reachable.

    Codex stamps `observed_at` from the rollout record it read even when every window in that
    record has lapsed -- the ordinary idle host -- so the row carried a stale-age trailer with
    no figure to be stale. A reader takes "as of 3d" as *there is a reading, and it is 3d old*,
    which is the opposite of the phrase beside it.
    """
    rows = (
        LimitRow("claude", (LimitWindow("5h", 34, "2h"),), None, None),
        LimitRow("codex", (), None, "3d", absence="no reading yet"),
    )
    _header, lines = _grid(rows)
    _claude, codex = lines

    assert "no reading yet" in codex
    assert "as of" not in codex, f"the row dates a reading it says it does not have: {codex!r}"

    # And the trailer is not simply gone from the render: a row that *does* have a stale
    # figure still says how old it is, which is the behaviour this must not have broken.
    dated = (LimitRow("claude", (LimitWindow("5h", 34, None),), None, "3d"),)
    _header, (line,) = _grid(dated)
    assert "as of 3d" in line


# --- the Claude row: a stored intention, drawn above the machine's own ----------------------
#
# The pane's last two lines are not about the account at all, and they are not about the same
# thing as each other. `Codex Remote Control` is a reading of a daemon that is running right
# now; `Claude Remote Control` is the intention stored in a file, which decides what the *next*
# pane comes up as. They are stacked rather than merged for that reason, and the order is the
# assertion: the stored intention is read before the live reading, so an owner scanning up from
# the bottom of the pane meets the machine first and what it will do next above it.
#
# These cases drive the real surface rather than `limit_rows_content`, because what they are
# about is the pane's composition -- which rows it adds and in which order -- and that is not
# a property of the grid renderer the rest of this file measures.


class _Launcher(SessionUseCaseDouble):
    """A host with no sessions: this pane's rows do not depend on any."""

    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return ()


class _FakeClaudeDefault:
    """The stored-default port, answering a reading or refusing to answer at all.

    The real port promises never to raise -- every way a settings file can be unreadable
    resolves to `PROVIDER_DEFAULT` -- so `fail` models the shapes it cannot promise about: a
    composition that wired something else, a read cancelled under teardown. That is the branch
    the pane's stale-not-wrong contract is about, and it is unreachable through the real port.
    """

    def __init__(self, value: RemoteControlDefault = RemoteControlDefault.ON) -> None:
        self.value = value
        self.fail = False
        self.reads = 0

    async def read(self) -> RemoteControlDefault:
        self.reads += 1
        if self.fail:
            raise RuntimeError("the settings file changed shape under an upgrade")
        return self.value


def _context(*, claude_default: object | None = None, limits=None) -> TuiContext:
    """A surface wired with the stored default, and with or without a limits reader.

    `replace` rather than a `backend_for` parameter, for the reason `test_settings_screen.py`
    gives: the support helper mirrors `Backend`'s fields by hand and this field is not among
    them, so stating it here keeps this file off a support-module edit it does not own.
    """
    backend = backend_for(
        sessions=_Launcher(),  # type: ignore[arg-type]
        projects=object(),  # type: ignore[arg-type]
        refresh_catalogue=lambda: (_CLAUDE_ROW_PROJECT,),
        catalogue=(_CLAUDE_ROW_PROJECT,),
        limits=limits,
    )
    return TuiContext(
        backend=replace(backend, claude_remote_control_default=claude_default),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )


_CLAUDE_ROW_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")


def _drawn(app: RemoteAgentsTui) -> list[tuple[str | None, str]]:
    """Every row of the limits pane as `(id, text)`, in the order it was added.

    Keyed by id as well as text because the ordering claim is structural: a row found by
    searching for its title would still be found if the pane drew it twice, or drew it in a
    line belonging to something else.
    """
    pane = app.screen.query_one("#limits-pane", OptionList)
    return [
        (pane.get_option_at_index(index).id, str(pane.get_option_at_index(index).prompt))
        for index in range(pane.option_count)
    ]


def _claude_row_and_codex_row(app: RemoteAgentsTui) -> tuple[str, str]:
    """The last two rows, asserted to be the two Remote Control lines in that order."""
    rows = _drawn(app)
    ids = [row_id for row_id, _ in rows]
    assert ids[-2:] == [_CLAUDE_REMOTE_CONTROL_ROW, _HOST_REMOTE_CONTROL_ROW], (
        f"the stored default must be the row directly above the machine's own, drew {ids}"
    )
    return rows[-2][1], rows[-1][1]


async def test_the_claude_row_is_drawn_directly_above_the_codex_row() -> None:
    """The dashboard's pane, which is what a bare `remote-agents tui` shows."""
    app = RemoteAgentsTui(_context(claude_default=_FakeClaudeDefault(RemoteControlDefault.OFF)))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        claude, codex = _claude_row_and_codex_row(app)

        assert claude == remote_control_default_line(RemoteControlDefault.OFF)
        assert codex.startswith(HOST_REMOTE_CONTROL_TITLE)


async def test_the_claude_row_sits_above_the_codex_row_on_the_console_pane_too() -> None:
    """The console's own limits pane, which is the surface under console hosting.

    Asserted separately rather than trusted to the mixin, because the two surfaces compose the
    pane themselves and a row added in one branch of `_draw_limits` and not the other would
    show up here and nowhere else.
    """
    app = RemoteAgentsTui(_context(claude_default=_FakeClaudeDefault(RemoteControlDefault.ON)))
    async with app.run_test() as pilot:
        await app.push_screen(LimitsPaneScreen())
        await pilot.pause()
        claude, _codex = _claude_row_and_codex_row(app)

        assert claude == remote_control_default_line(RemoteControlDefault.ON)


async def test_an_unwired_claude_row_says_unavailable_rather_than_going_missing() -> None:
    """A composition with no Claude provider has declared an absence (DEC-009/DEC-061).

    The row is still drawn, because a missing row is indistinguishable from a surface that
    forgot to draw one -- and the word is the application's own, so the two rows of the
    settings screen and this one cannot drift apart on how they spell it (DEC-007).
    """
    app = RemoteAgentsTui(_context(claude_default=None))
    async with app.run_test() as pilot:
        await pilot.pause()
        claude, _codex = _claude_row_and_codex_row(app)

        assert claude == f"{REMOTE_CONTROL_DEFAULT_TITLE} · {UNAVAILABLE}"


async def test_a_failing_read_leaves_the_claude_row_exactly_as_it_was_drawn() -> None:
    """Stale, not wrong: a background read having a bad moment never blanks a drawn line.

    The same contract `_reload_limits` keeps for the grid and `_reload_host_remote_control`
    keeps for the machine's line, asserted here because a third read is a third chance to get
    it wrong -- and the wrong version (clearing the reading on `except`) would repaint this row
    as `unavailable`, which states that no Claude provider is wired at all.
    """
    port = _FakeClaudeDefault(RemoteControlDefault.OFF)
    app = RemoteAgentsTui(_context(claude_default=port))
    async with app.run_test() as pilot:
        await pilot.pause()
        claude, _codex = _claude_row_and_codex_row(app)
        assert claude == remote_control_default_line(RemoteControlDefault.OFF)

        port.fail = True
        await app.screen._reload_limits()
        await pilot.pause()

        claude, _codex = _claude_row_and_codex_row(app)
        assert claude == remote_control_default_line(RemoteControlDefault.OFF), (
            "a read that raised took the last good reading off the pane"
        )


async def test_the_claude_row_survives_a_host_that_wired_no_limits_reader() -> None:
    """The third read is not conditional on the other two.

    Three separate capabilities, and a host wiring one and not the others is an ordinary
    composition rather than a broken one -- so an early return on an absent `limits` would take
    this row off a pane that could still draw it. The empty sentence is the *other* branch of
    `_draw_limits`, which is exactly where a row added in one branch only goes missing.
    """
    app = RemoteAgentsTui(
        _context(claude_default=_FakeClaudeDefault(RemoteControlDefault.PROVIDER_DEFAULT))
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = _drawn(app)
        assert [row_id for row_id, _ in rows] == [
            _EMPTY_LIMITS_ROW,
            _CLAUDE_REMOTE_CONTROL_ROW,
            _HOST_REMOTE_CONTROL_ROW,
        ], rows

        claude, _codex = _claude_row_and_codex_row(app)
        assert claude == remote_control_default_line(RemoteControlDefault.PROVIDER_DEFAULT)


async def test_a_pushed_claude_row_reading_is_drawn_without_a_second_read() -> None:
    """`show_claude_remote_control_default` draws a pushed reading without re-reading.

    **This method has no production caller**, and saying so here is the point: a green test
    over dead code is worse than no test, because it reads as proof the method is wired into a
    live flow. It is not. `show_host_remote_control`, which it is shaped after, is called by
    `RemoteAgentsTui.set_host_remote_control` because the *app* issues that write; Claude's
    stored default is written by `SettingsScreen` on its own screen and never reaches this
    region. Nothing goes stale, because Settings is pushed over the dashboard and the escape
    back fires `on_reveal`, which re-reads.

    What this pins is therefore the method's contract rather than a live path -- draw the value
    handed in, do not ask the port again -- so that if a later surface does acquire a position
    that writes without passing through `on_reveal`, the seam it would push through already
    behaves. `dashboard.py`'s own docstring carries the same admission and the condition under
    which the honest change is to delete both.
    """
    port = _FakeClaudeDefault(RemoteControlDefault.OFF)
    app = RemoteAgentsTui(_context(claude_default=port))
    async with app.run_test() as pilot:
        await pilot.pause()
        reads = port.reads

        app.screen.show_claude_remote_control_default(RemoteControlDefault.ON)
        await pilot.pause()

        claude, _codex = _claude_row_and_codex_row(app)
        assert claude == remote_control_default_line(RemoteControlDefault.ON)
        assert port.reads == reads, "the pushed reading was drawn, not re-read"


# --- one fixed column set (0.46.0) ----------------------------------------------------------
#
# The owner saw Codex's `week` and `5h` swap whenever Claude's five-hour window lapsed: the
# columns were collected first-seen across every row, and Claude's row is read first. The
# columns are now a fixed list by window kind, so no row's state can reorder another's.


def _claude_week_only_beside_codex_both() -> tuple[LimitRow, ...]:
    return (
        LimitRow("claude", (LimitWindow("week", 61, "3d"),), None, None),
        LimitRow("codex", (LimitWindow("5h", 3, "4h"), LimitWindow("week", 40, "5d")), None, None),
    )


@pytest.mark.parametrize("width", [120, 80, 60])
def test_codex_order_does_not_follow_claude(width: int) -> None:
    """Claude with only its week beside Codex with both: Codex still reads `5h` then `wk`."""
    lines = [
        content.plain
        for content in limit_rows_content(_claude_week_only_beside_codex_both(), width)
    ]
    text = "\n".join(lines)
    if lines[0].split()[0] == "5h":
        # One-line layout: the header names the columns, and codex's 5h figure (3%) sits under
        # `5h`, left of its week figure (40%) under `week`.
        header, codex = lines[0], next(line for line in lines if line.startswith("codex"))
        assert header.index("5h") < header.index("week"), f"at width {width}:\n{text}"
        assert codex.index("3%") < codex.index("40%"), f"at width {width}:\n{text}"
    else:
        codex = text[text.index("codex") :]
        assert codex.index("5h") < codex.index("wk"), f"at width {width}:\n{text}"


def test_a_missing_window_draws_its_label_and_an_empty_bar() -> None:
    """Claude's lapsed five-hour window is its label, an empty bar, and no figure at all."""
    header, (claude, codex) = _grid(_claude_week_only_beside_codex_both())
    cell = claude[header.index("5h") : header.index("week")]
    assert "░" * 8 in cell, f"the missing window has no empty bar: {claude!r}"
    figures = cell[cell.index("░") :]
    assert not re.search(r"[0-9%]", figures), f"the missing window shows a figure: {cell!r}"
    assert _labelled_gauges(header, claude).keys() == _labelled_gauges(header, codex).keys()
    assert _gauge_spans(claude) == _gauge_spans(codex)


_KINDS = ((), ("5h",), ("week",), ("5h", "week"))


@pytest.mark.parametrize("first", _KINDS, ids=lambda kinds: "+".join(kinds) or "none")
@pytest.mark.parametrize("second", _KINDS, ids=lambda kinds: "+".join(kinds) or "none")
def test_every_row_draws_every_column(first: tuple[str, ...], second: tuple[str, ...]) -> None:
    """Every combination of present and absent windows for two agents: same columns, same place."""

    def row(profile: str, kinds: tuple[str, ...]) -> LimitRow:
        windows = tuple(LimitWindow(kind, 50, "1h") for kind in kinds)
        return LimitRow(profile, windows, None, None, absence=None if windows else "no reading yet")

    header, lines = _grid((row("claude", first), row("codex", second)))
    assert len(lines) == 2, "\n".join(lines)
    offsets = _column_offsets(header, lines)
    assert list(offsets) == ["5h", "week"], "\n".join((header, *lines))
    for label, starts in offsets.items():
        assert len(starts) == 1, f"{label} is drawn at {sorted(starts)}.\n" + "\n".join(lines)
    assert header.index("5h") < header.index("week"), header


# --- the pace tick (DEC-106) ----------------------------------------------------------------
#
# A week or day window with a live reading carries where an even spend would stand today, and
# the gauge shows it as a `┃` in its bar. A glyph, not a colour change (DEC-010): the fill on
# both sides keeps its threshold colour, so the tick alone marks the boundary.

_TICKED_GAUGE = re.compile(r"[█░┃]+")


def _style_at(content, index: int) -> str:
    """The style of the one cell at `index`, read off the spans that cover it."""
    styles = [str(span.style) for span in content.spans if span.start <= index < span.end]
    assert len(styles) == 1, f"cell {index} is covered by {styles}"
    return styles[0]


@pytest.mark.parametrize(
    ("expected", "cells", "index"),
    [
        (0, 8, 0),
        (14, 8, 1),
        (57, 8, 5),
        (100, 8, 7),
        (0, 16, 0),
        (14, 16, 2),
        (57, 16, 9),
        (100, 16, 15),
    ],
)
def test_the_pace_tick_sits_at_the_expected_share(expected: int, cells: int, index: int) -> None:
    """`round(expected/100 * cells)`, clamped to the last cell so 100% still draws inside."""
    bar = limit_gauge_content(40, cells, expected).plain
    assert len(bar) == cells, bar
    assert bar.count("┃") == 1 and bar.index("┃") == index, bar


def test_a_five_hour_gauge_never_has_a_pace_tick() -> None:
    """Fed from the application, so the rule under test is the whole path's, not a fixture's."""
    soon = datetime.now(UTC) + timedelta(hours=2)
    later = datetime.now(UTC) + timedelta(days=6)
    rows = limit_rows(
        (
            AgentLimits(
                ProfileId("claude"),
                (
                    UsageWindow("5h", 40.0, resets_at=soon),
                    UsageWindow("week", 7.0, resets_at=later),
                ),
            ),
        )
    )
    lines = [content.plain for content in limit_rows_content(rows, 80)]
    gauges = [match.group() for line in lines for match in _TICKED_GAUGE.finditer(line)]
    assert len(gauges) == 2, lines
    five_hour, week = gauges
    assert "┃" not in five_hour, lines
    assert week.count("┃") == 1, lines


def _one_paced_row() -> tuple[LimitRow, ...]:
    return (
        LimitRow(
            "claude",
            (
                LimitWindow("5h", 28, "1h"),
                LimitWindow("week", 71, "3d", expected_percent=57, pace_delta=14),
            ),
            None,
            None,
        ),
    )


def test_the_week_gauge_widens_with_the_pane_for_its_pace_tick() -> None:
    """16 cells from a 70-cell pane up, the 8 `_GAUGE_CELLS` below it; the 5h gauge never widens."""
    for width, week_cells in ((70, 16), (69, 8), (120, 16)):
        lines = [content.plain for content in limit_rows_content(_one_paced_row(), width)]
        gauges = [match.group() for line in lines for match in _TICKED_GAUGE.finditer(line)]
        assert [len(gauge) for gauge in gauges] == [8, week_cells], (width, lines)


def test_the_pace_tick_styles() -> None:
    """The tick is `$text`; fill on both sides of it keeps one threshold colour; the track
    stays `$secondary`."""
    content = limit_gauge_content(71, 16, 57)
    bar = content.plain
    assert bar == "█████████┃██░░░░", bar
    tick = bar.index("┃")
    assert _style_at(content, tick) == "$text"
    before, after = _style_at(content, tick - 1), _style_at(content, tick + 1)
    assert before == after == "$warning"
    assert _style_at(content, len(bar) - 1) == "$secondary"


# --- the header row and the pace columns (DEC-106, DEC-100) ---------------------------------


def _readme_rows() -> tuple[LimitRow, ...]:
    """The handoff README's two example rows."""
    return (
        LimitRow(
            "claude",
            (
                LimitWindow("5h", 28, "1h"),
                LimitWindow("week", 7, "6d", expected_percent=14, pace_delta=-7),
            ),
            None,
            None,
        ),
        LimitRow(
            "codex",
            (
                LimitWindow("5h", 18, "1h"),
                LimitWindow("week", 71, "3d", expected_percent=57, pace_delta=14),
            ),
            None,
            None,
        ),
    )


def test_the_pace_columns_are_a_property_of_the_set() -> None:
    """Widths measured across rows: a row with no pace leaves both cells blank at their widths,
    and every row's column offsets are identical."""
    paced, _codex = _readme_rows()
    unpaced = LimitRow(
        "a-longer-name",
        (LimitWindow("5h", 3, "4h"), LimitWindow("week", 100, "1d")),
        None,
        None,
    )
    for rows in ((paced, unpaced), (unpaced, paced)):
        header, *lines = [content.plain for content in limit_rows_content(rows, WIDE)]
        assert len(lines) == 2, lines
        starts = {line.index("%") for line in lines}
        assert len(starts) == 1, "\n".join((header, *lines))
        (paced_line,) = [line for line in lines if line.startswith("claude")]
        (unpaced_line,) = [line for line in lines if not line.startswith("claude")]
        expected_end = header.index("expected") + len("expected")
        assert paced_line[expected_end - 3 : expected_end] == "14%", paced_line
        assert paced_line[header.index("vs pace") :] == "▼ 7 under", paced_line
        assert unpaced_line.rstrip() == unpaced_line[: header.index("expected")].rstrip(), (
            "a row with no pace draws nothing in either pace column"
        )


def test_pace_text_words() -> None:
    assert pace_text(0) == "on pace"
    assert pace_text(14) == "▲ 14 over"
    assert pace_text(-7) == "▼ 7 under"


def test_pace_style_thresholds() -> None:
    assert [pace_style(delta) for delta in (-30, -1, 0)] == ["$success"] * 3
    assert [pace_style(delta) for delta in (1, 25)] == ["$warning"] * 2
    assert pace_style(26) == "$error"


def test_the_header_row_labels_each_column() -> None:
    """`5h` and `week` over their bars, `expected` right-aligned over its figures, `vs pace` over
    its words -- and the rows below carry no labels of their own."""
    header, claude, codex = [content.plain for content in limit_rows_content(_readme_rows(), 80)]
    for line in (claude, codex):
        gauges = [match.start() for match in _TICKED_GAUGE.finditer(line)]
        assert gauges == [header.index("5h"), header.index("week")], (header, line)
        assert header.index("expected") + len("expected") == line.index("%", gauges[1] + 20) + 1
        assert line[header.index("vs pace")] in "▲▼o", (header, line)
        assert " 5h " not in line and " wk " not in line, line
    (styles,) = {str(span.style) for span in limit_rows_content(_readme_rows(), 80)[0].spans}
    assert styles == "$text-muted"


def test_table_width_counts_the_pace_columns() -> None:
    """The stack decision flips at exactly the width of the header row."""
    header = limit_rows_content(_readme_rows(), WIDE)[0].plain
    width = len(header.rstrip()) + len("▲ 14 over") - len("vs pace")
    assert len(limit_rows_content(_readme_rows(), width)) == 3, "fits: header and two rows"
    stacked = [content.plain for content in limit_rows_content(_readme_rows(), width - 1)]
    assert "expected" not in stacked[0], stacked


def test_the_readme_example_rows_at_width_80() -> None:
    """The handoff's two rows, cell for cell as this pane draws them.

    Two things differ from the README's hand-typed text, both on purpose and recorded in the
    plan: the percent and reset columns keep widths measured across the set (`3` and `4` here,
    where the mock reserves `100%` and `↻ 59m`), and the fill counts are `percent_gauge`'s own
    rounding-up, which the mock's hand-drawn bars do not follow consistently.
    """
    lines = [content.plain for content in limit_rows_content(_readme_rows(), 80)]
    assert lines == [
        "        5h                 week                       expected  vs pace",
        "claude  ███░░░░░ 28% ↻ 1h  ██┃░░░░░░░░░░░░░  7% ↻ 6d       14%  ▼ 7 under",
        "codex   ██░░░░░░ 18% ↻ 1h  █████████┃██░░░░ 71% ↻ 3d       57%  ▲ 14 over",
    ]
