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
