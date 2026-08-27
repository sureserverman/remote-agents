"""Deterministic, safe Telegram view models for the private control surface."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from remote_agents.ports.terminal_text import encodable_text

MAX_TELEGRAM_TEXT_UNITS = 4096
_ELLIPSIS = "…"


@dataclass(frozen=True, slots=True)
class Button:
    """A Telegram keyboard button whose callback is a server-side lookup token."""

    text: str
    callback_data: str


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    """A stable text-and-keyboard snapshot suitable for an edited Telegram message."""

    text: str
    keyboard: tuple[tuple[Button, ...], ...]


def render_message(text: str, keyboard: tuple[tuple[Button, ...], ...] = ()) -> RenderedMessage:
    """Create the sole typed Telegram screen model after enforcing its text budget."""

    return _message(text, keyboard)


def bounded_text(text: str, *, limit: int = MAX_TELEGRAM_TEXT_UNITS) -> str:
    """Return text that fits Telegram's UTF-16 message budget without splitting a character."""

    if limit < 1:
        raise ValueError("Telegram text limit must be positive")
    text = encodable_text(text)
    if _utf16_units(text) <= limit:
        return text

    suffix_units = _utf16_units(_ELLIPSIS)
    kept: list[str] = []
    used = 0
    for character in text:
        character_units = _utf16_units(character)
        if used + character_units + suffix_units > limit:
            break
        kept.append(character)
        used += character_units
    return "".join(kept) + _ELLIPSIS


def _message(text: str, keyboard: tuple[tuple[Button, ...], ...]) -> RenderedMessage:
    # The last gate every screen passes, and the one that makes the guarantee hold for text
    # this module did not compose: `service` builds most of its screens with f-strings around
    # `escape(...)`, which is HTML-safe but says nothing about what UTF-16 can carry.
    text = encodable_text(text)
    if _utf16_units(text) > MAX_TELEGRAM_TEXT_UNITS:
        raise ValueError("presenter text exceeds the Telegram message limit")
    return RenderedMessage(text=text, keyboard=keyboard)


def _validate_callback(callback: str) -> None:
    """Refuse anything that is not one of this bot's own opaque tokens.

    Kept when `NavigationCallbacks` and its plural validator were deleted with the rest of
    the unused presenters: this one has a live caller in `notifications.render_activity`,
    which is handed an already-minted token and checks it rather than trusting it.
    """
    encoded = callback.encode("utf-8")
    if not callback.startswith("c1_") or not 1 <= len(encoded) <= 64 or not callback.isascii():
        raise ValueError("Telegram navigation callbacks must be opaque c1_ tokens")


def _utf16_units(text: str) -> int:
    # Total, because `encodable_text` has already removed everything an encoder would refuse.
    # Without it a lone surrogate — from a hook payload or from an undecodable directory name
    # — raised `UnicodeEncodeError` out of the middle of a render, and every budget in this
    # module runs through this one line.
    return len(encodable_text(text).encode("utf-16-le")) // 2


def _bounded_escaped(text: str, limit: int) -> str:
    """Escape raw display text while fitting the final HTML into a UTF-16 budget."""

    if limit < 1:
        return ""
    text = encodable_text(text)
    escaped = escape(text)
    if _utf16_units(escaped) <= limit:
        return escaped

    suffix_units = _utf16_units(_ELLIPSIS)
    kept: list[str] = []
    used = 0
    for character in text:
        safe_character = escape(character)
        character_units = _utf16_units(safe_character)
        if used + character_units + suffix_units > limit:
            break
        kept.append(safe_character)
        used += character_units
    return "".join(kept) + _ELLIPSIS


UNIFORM_ROW_WIDTH = 44
"""The character width every screen's keyboard is widened to reach, when it falls short.

Telegram gives an inline keyboard no width of its own: it sizes to its widest row, and every
*other* row then stretches to match. That is why some screens read narrow and some wide even
though all of them close with the same navigation bar — a sessions page carries a row like
`remote-agents · claude · fresh · #7 · running · 3h` and saturates the available width, while
`That session is no longer available.` above a `Back` row and a three-word bar has nothing in
it wider than fourteen characters and renders as a small box in the middle of the chat.

So the fix is a floor, not a cap. One row per screen is padded up to this width and no row is
ever shortened, which is what keeps it total: a screen whose widest row already exceeds the
floor is left exactly as it was, and a screen that falls short is brought up to the same width
as its neighbours. Nothing here can make two screens *disagree* more than they already did.

Forty-four is the width a single-button row needs to fill a phone-width keyboard, measured
against the rows that already saturate rather than chosen for roundness. It is the one number
to change if the floor turns out to sit wrong, and changing it cannot break anything: it is
read in exactly one place.
"""

KEYBOARD_PADDING = "⠀"
"""BRAILLE PATTERN BLANK — padding Telegram will not trim.

An ordinary space cannot do this job. The Bot API strips leading and trailing whitespace from
a button's `text`, so a label padded with spaces arrives back at its original width and the
keyboard is unchanged. U+2800 is a printable character in category `So` that happens to render
as blank in every font that carries Braille, which is the property being borrowed; it survives
the trim because as far as the API is concerned it is content.

It is also printable under `str.isprintable()`, so it passes the same filters this project
applies to text it did not write, and it is left out of `_bounded_escaped` and `bounded_text`
entirely — those bound *message* text, and this never appears there.

Public, with `unpadded` beside it, because the padding is applied *after* a screen is composed
and anything reading a keyboard back off the wire — a test asserting which buttons a screen
carries, most of all — is reading a label this module widened rather than one a screen builder
wrote. Making that reversible is cheaper than making every such reader know the character.
"""


def unpadded(label: str) -> str:
    """Recover the label a screen builder wrote from the one the keyboard was sent with.

    The inverse of what `uniform_keyboard` adds, and safe to apply to any label: padding is
    only ever appended, and no screen in this bot ends a label with a Braille blank of its own.
    """
    return label.rstrip(KEYBOARD_PADDING)


def uniform_keyboard(
    keyboard: tuple[tuple[Button, ...], ...], *, width: int = UNIFORM_ROW_WIDTH
) -> tuple[tuple[Button, ...], ...]:
    """Widen one row of a keyboard so every screen's keyboard reaches the same floor.

    The **last** row is the one padded, because on every screen built through
    `service._message` that row is the fixed navigation bar (DEC-032) — present on all of them,
    never the row carrying an agent's own words, and already the widest thing on the screens
    that are too narrow. Padding a row the owner reads for content would put invisible
    characters inside a session's name; padding the bar puts them inside three fixed words.

    The deficit is spread across that row's buttons rather than dropped on one, so a two- or
    three-button bar stays visually even instead of gaining one stretched cell.

    Only callbacks are preserved verbatim — the token is the thing a press is looked up by, and
    nothing here may touch it.
    """
    if width < 1:
        raise ValueError("uniform keyboard width must be positive")
    if not keyboard:
        return keyboard
    if max(sum(len(button.text) for button in row) for row in keyboard) >= width:
        return keyboard
    *leading, last = keyboard
    if not last:
        return keyboard
    deficit = width - sum(len(button.text) for button in last)
    share, remainder = divmod(deficit, len(last))
    padded = tuple(
        Button(
            button.text + KEYBOARD_PADDING * (share + (1 if index < remainder else 0)),
            button.callback_data,
        )
        for index, button in enumerate(last)
    )
    return (*leading, padded)
