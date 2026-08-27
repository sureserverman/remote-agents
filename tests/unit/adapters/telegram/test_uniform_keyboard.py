"""The keyboard width floor: what it changes, what it must never change."""

from __future__ import annotations

from remote_agents.adapters.telegram.presenters import (
    KEYBOARD_PADDING,
    UNIFORM_ROW_WIDTH,
    Button,
    uniform_keyboard,
    unpadded,
)


def _width(row: tuple[Button, ...]) -> int:
    return sum(len(button.text) for button in row)


def _bar() -> tuple[Button, ...]:
    return (
        Button("Sessions", "c1_sessions"),
        Button("Launch", "c1_launch"),
        Button("Resume", "c1_resume"),
    )


def test_a_narrow_screen_is_widened_to_the_floor() -> None:
    keyboard = ((Button("Back", "c1_back"),), _bar())

    widened = uniform_keyboard(keyboard)

    assert _width(widened[-1]) == UNIFORM_ROW_WIDTH


def test_a_screen_that_already_reaches_the_floor_is_left_exactly_alone() -> None:
    """The floor is a minimum, never a target — a wide screen must come back byte-identical."""
    row = (Button("remote-agents · claude · regular · #7 · running · 3h", "c1_row"),)
    keyboard = (row, _bar())

    assert uniform_keyboard(keyboard) is keyboard


def test_the_deficit_is_spread_across_the_row_rather_than_dropped_on_one_button() -> None:
    """Otherwise a three-button bar gains one stretched cell and two normal ones."""
    widened = uniform_keyboard(((Button("Back", "c1_back"),), _bar()))

    widths = [len(button.text) for button in widened[-1]]
    assert max(widths) - min(widths) <= 2


def test_padding_never_touches_a_callback_token() -> None:
    """The token is what a press is looked up by; widening a screen may not reach it."""
    keyboard = ((Button("Back", "c1_back"),), _bar())

    widened = uniform_keyboard(keyboard)

    assert [button.callback_data for row in widened for button in row] == [
        button.callback_data for row in keyboard for button in row
    ]


def test_only_the_last_row_is_padded() -> None:
    """The navigation bar closes every screen, so padding it never touches an agent's words."""
    rows = ((Button("Inspect", "c1_inspect"),), (Button("Rename", "c1_rename"),), _bar())

    widened = uniform_keyboard(rows)

    assert widened[:-1] == rows[:-1]


def test_every_padded_label_reads_back_as_the_label_a_screen_builder_wrote() -> None:
    widened = uniform_keyboard(((Button("Back", "c1_back"),), _bar()))

    assert [unpadded(button.text) for button in widened[-1]] == ["Sessions", "Launch", "Resume"]


def test_the_padding_character_is_not_whitespace_telegram_would_trim() -> None:
    """An ordinary space is stripped from a button's text by the Bot API, so this cannot be one."""
    assert not KEYBOARD_PADDING.isspace()
    assert KEYBOARD_PADDING.isprintable()


def test_an_empty_keyboard_stays_empty() -> None:
    """A screen with no keyboard is a real screen — the pending launch view is one."""
    assert uniform_keyboard(()) == ()


def test_a_row_of_no_buttons_is_not_padded_into_existence() -> None:
    assert uniform_keyboard(((Button("Back", "c1_back"),), ())) == (
        (Button("Back", "c1_back"),),
        (),
    )
