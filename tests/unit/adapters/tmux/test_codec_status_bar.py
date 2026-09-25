"""The console's function-key bar: one `status-format[0]`, built from the key table and the theme.

Sub-plan 2 (console facelift) Task 1.4. The format is checked twice: as a string, for the
properties no rendering can show (numeric width switch, no shell-out, colours from the theme),
and through a real tmux client, for the text the owner actually reads at 200 and 100 columns.

The real-tmux half attaches a nested client inside an outer scratch pane, as
`tests/integration/tmux/test_status_bar_probe.py` does and for the reason it records: only a
real client has a `client_width`, and only its terminal shows the status row.
"""

from __future__ import annotations

import re
import time

import pytest
from bar_console import BarConsole

from remote_agents.adapters.tmux.codec import CONSOLE_SESSION_NAME, status_format_args
from remote_agents.adapters.tui.keys import BAR_KEYS, FUNCTION_KEYS, status_bar_keys
from remote_agents.adapters.tui.theme import THEMES, status_bar_palette
from remote_agents.ports.console import (
    REMOTE_CONTROL_COMPACT_OPTION,
    REMOTE_CONTROL_OPTION,
    SESSION_SELECTED_OPTION,
    TYPING_OPTION,
)

_NIGHT, _DAY = (status_bar_palette(theme) for theme in THEMES)

#: The design's two lines (handoff README § 2), keys half, text only.
_FULL_KEYS = (
    "1 help  2 settings  3 inspect  4 detail  5 refresh  6 rename  7 add project  8 stop  "
    "9 force stop  10 close console  11 terminal  12 projects"
)
_COMPACT_KEYS = (
    "1help 2setup 3view 4detail 5refresh 6rename 7addproj 8stop 9kill 10close 12projects"
)

#: What `remote_control_words_args` publishes for "claude on · codex on", written by hand here
#: so this file checks the format and not the publisher (Task 1.5 checks that).
_RC_ON_FULL = "Remote Control  claude on · codex on"


def _format(palette=_NIGHT) -> str:
    (value,) = [
        argv[-1]
        for argv in status_format_args(status_bar_keys(), palette)
        if argv[-2] == "status-format[0]"
    ]
    return value


def _options(palette=_NIGHT) -> dict[str, str]:
    return {argv[-2]: argv[-1] for argv in status_format_args(status_bar_keys(), palette)}


# --- the string ------------------------------------------------------------------------------


def test_the_bar_names_every_function_key_in_order() -> None:
    """Built from the table: every label and every short, in key order, none spelled here.

    Order on screen is proven by the real-client lines below; this pins the source.
    """
    keys = status_bar_keys()
    text = re.sub(r"#\[[^]]*\]", "", _format())

    assert [entry.number for entry in keys] == [int(entry.key[1:]) for entry in BAR_KEYS]
    assert [entry.short for entry in keys if entry.bound] == [e.short for e in FUNCTION_KEYS]
    for entry in keys:
        assert f"{entry.number} {entry.label}" in text, entry.label
        if entry.bound:
            assert f"{entry.number}{entry.short}" in text, entry.short
    assert "10 close console" in text, "F10 reads the console's own label (DEC-096)"


def test_the_width_switch_is_numeric() -> None:
    value = _format()

    assert "#{e|>|:#{client_width},160}" in value
    assert not re.search(r"#\{(>|<|>=|<=):", value)


def test_colours_come_from_the_theme() -> None:
    for palette in (_NIGHT, _DAY):
        values = " ".join(_options(palette).values())
        used = set(re.findall(r"(?:fg|bg)=(#[0-9A-Fa-f]{6})", values))
        allowed = {palette.bar, palette.text, palette.key, palette.dim, palette.muted}

        assert used, "the bar styles nothing"
        assert used <= allowed, used - allowed
        assert not re.search(r"(?:fg|bg)=(?!#)", values), "a tmux colour name is written"
    assert _format(_NIGHT) != _format(_DAY)


def test_status_every_theme_the_palette_offers_yields_a_bar() -> None:
    """Textual's own themes leave `panel` and our greys unset; the bar must still resolve."""
    from textual.theme import BUILTIN_THEMES

    for theme in BUILTIN_THEMES.values():
        assert status_format_args(status_bar_keys(), status_bar_palette(theme))


def test_no_shell_out() -> None:
    assert "#(" not in " ".join(_options().values())


def test_the_bar_is_the_console_session_s_and_nothing_wider() -> None:
    """Session options on the console session: no `-g`, and nothing targets another."""
    for argv in status_format_args(status_bar_keys(), _NIGHT):
        assert argv[0] == "set-option"
        assert "-g" not in argv
        assert argv[argv.index("-t") + 1] == f"{CONSOLE_SESSION_NAME}:"
    assert _options()["status"] == "on"
    assert _options()["status-position"] == "bottom"
    assert _options()["status-interval"] == "0"
    assert _options()["status-style"] == f"bg={_NIGHT.bar},fg={_NIGHT.text}"


def test_session_keys_dim_on_no_selection() -> None:
    value = _format()
    for entry in status_bar_keys():
        if entry.needs_selection:
            assert f"#{{!=:#{{{SESSION_SELECTED_OPTION}}},1}}" in value
    assert {entry.number for entry in status_bar_keys() if entry.needs_selection} == {3, 4, 6, 8, 9}


def test_stop_keys_dim_while_typing() -> None:
    assert f"#{{==:#{{{TYPING_OPTION}}},1}}" in _format()
    typing = {entry.number for entry in status_bar_keys() if entry.refused_while_typing}
    assert typing == {2, 7, 8, 9}


# --- a real tmux client ----------------------------------------------------------------------


def _style_of(expanded: str, number: int, words: str) -> str:
    """The `fg=` a key's number is drawn in, in an expanded format.

    A lit key carries a style between its number and its words (`3#[fg=…] inspect`), and a dim
    one does not (`3 inspect`), so the key is found with or without one.
    """
    found = re.search(rf"(?<![0-9]){number}(?:#\[[^]]*\])?{re.escape(words)}", expanded)
    assert found, (number, words)
    return re.findall(r"#\[fg=(#[0-9A-Fa-f]{6})\]", expanded[: found.start()])[-1]


@pytest.fixture
def console():
    made: list[BarConsole] = []

    def make(width: int) -> BarConsole:
        made.append(BarConsole(width))
        return made[-1]

    yield make
    for each in made:
        each.close()


def test_status_full_line_at_200_selected_and_rc_on(console) -> None:
    bar = console(200)
    bar.start()
    bar.set(SESSION_SELECTED_OPTION, "1")
    bar.set(REMOTE_CONTROL_OPTION, _RC_ON_FULL)

    row = bar.settled_row(lambda row: "codex on" in row)

    assert len(row) == 200
    assert row.startswith(_FULL_KEYS + " ")
    assert row.endswith(f"{_RC_ON_FULL}  {CONSOLE_SESSION_NAME}"), repr(row)


def test_status_compact_line_at_100_has_no_f11(console) -> None:
    bar = console(100)
    bar.start()
    bar.set(REMOTE_CONTROL_COMPACT_OPTION, "RC ●●")

    row = bar.settled_row(lambda row: "RC" in row)

    assert row.startswith(_COMPACT_KEYS + " ")
    assert "11" not in row
    assert row.rstrip().endswith("RC ●●"), repr(row)


@pytest.mark.parametrize("width", [200, 100])
def test_status_session_keys_take_the_dim_colour_with_nothing_selected(console, width) -> None:
    bar = console(width)
    bar.start()
    full = width > 160

    bar.set(SESSION_SELECTED_OPTION, "0")
    unselected = bar.expanded(_format())
    bar.set(SESSION_SELECTED_OPTION, "1")
    selected = bar.expanded(_format())

    for entry in status_bar_keys():
        if not entry.bound and not full:
            continue
        words = f" {entry.label}" if full else entry.short
        want_dim = entry.needs_selection or not entry.bound
        assert (_style_of(unselected, entry.number, words) == _NIGHT.dim) is want_dim, words
        assert (_style_of(selected, entry.number, words) == _NIGHT.dim) is (not entry.bound), words


@pytest.mark.parametrize("width", [200, 100])
def test_status_typing_dims_f2_f7_f8_f9_and_says_esc_cancels(console, width) -> None:
    bar = console(width)
    bar.start()
    bar.set(SESSION_SELECTED_OPTION, "1")
    bar.set(REMOTE_CONTROL_OPTION, _RC_ON_FULL)
    bar.set(REMOTE_CONTROL_COMPACT_OPTION, "RC ●●")
    bar.set(TYPING_OPTION, "1")

    row = bar.settled_row(lambda row: "esc cancels" in row)
    expanded = bar.expanded(_format())

    assert "esc cancels" in row
    assert "Remote Control" not in row and "RC" not in row
    full = width > 160
    for entry in status_bar_keys():
        if not entry.bound:
            continue
        words = f" {entry.label}" if full else entry.short
        dim = _style_of(expanded, entry.number, words) == _NIGHT.dim
        assert dim is entry.refused_while_typing, words


@pytest.mark.parametrize("marks", ["RC ●●", "RC ○●", "RC ?○"])
def test_status_compact_rc_marks_are_drawn_as_published(console, marks) -> None:
    bar = console(100)
    bar.start()
    bar.set(REMOTE_CONTROL_COMPACT_OPTION, marks)

    assert bar.settled_row(lambda row: marks in row).rstrip().endswith(marks)


def test_status_nothing_published_draws_no_remote_control_claim(console) -> None:
    bar = console(200)
    bar.start()

    row = bar.status_row()

    assert "Remote Control" not in row
    assert row.rstrip().endswith(CONSOLE_SESSION_NAME)


# --- the right end always fits (found by the Task 2.4 live drill) --------------------------------

_RC_LONG_FULL = "Remote Control  claude Claude's default · codex unreachable"


def _whole_right_ends(full: bool, typing: bool, words: str) -> set[str]:
    """Every right end the bar may draw: whole versions only, longest first, or nothing."""
    session = CONSOLE_SESSION_NAME
    if typing:
        return {f"esc cancels  {session}", "esc cancels", ""} if full else {"esc cancels", ""}
    if full:
        return {f"{words}  {session}", f"RC ??  {session}", "RC ??", session, ""}
    return {"RC ??", ""}


@pytest.mark.parametrize("width", [84, 100, 130, 160, 161, 165, 170, 185, 200, 215, 240])
@pytest.mark.parametrize("words", [_RC_ON_FULL, _RC_LONG_FULL])
@pytest.mark.parametrize("typing", [False, True])
def test_status_right_end_is_whole_or_absent_at_every_width(console, width, words, typing) -> None:
    """At any width the right end is one whole candidate, never a fragment clipped at its left.

    The live drill found `12 projectsol  claude …`: at 200 columns the full keys take 145 cells,
    and `Claude's default · codex unreachable` does not fit beside them, so tmux cut the right
    end from the left. The class is every right end against every width, so it is swept here.
    """
    full = width > 160
    bar = console(width)
    bar.start()
    bar.set(REMOTE_CONTROL_OPTION, words)
    bar.set(REMOTE_CONTROL_COMPACT_OPTION, "RC ??")
    bar.set(TYPING_OPTION, "1" if typing else "0")

    keys = _FULL_KEYS if full else _COMPACT_KEYS
    bar.settled_row(lambda row: row.startswith(keys))
    time.sleep(0.05)  # the three option writes each redraw; read after the last
    row = bar.status_row()

    assert row.startswith(keys), row
    right = row[len(keys) :].strip()
    assert right in _whole_right_ends(full, typing, words), (width, row)
    if full and not typing and width >= len(keys) + 2 + len(words) + 2 + len(CONSOLE_SESSION_NAME):
        assert right == f"{words}  {CONSOLE_SESSION_NAME}", "the words fit and were not drawn"


def test_the_console_window_s_pane_dividers_take_the_bar_s_colour() -> None:
    """tmux draws its own dividers between the panes; untuned they are ANSI green and grey
    (the Stage 2 fidelity finding M2, round 2). They take the bar's panel colour, as window
    options on the console window, so the gutters recede as the mock draws them."""
    args = status_format_args(status_bar_keys(), _NIGHT)
    for option in ("pane-border-style", "pane-active-border-style"):
        found = [argv for argv in args if option in argv]
        assert found == [("set-option", "-w", "-t", "ra-console:", option, f"fg={_NIGHT.bar}")], (
            found
        )
