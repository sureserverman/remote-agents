"""Deterministic, safe Telegram view models for the private control surface."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from math import ceil

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


@dataclass(frozen=True, slots=True)
class NavigationCallbacks:
    """Opaque callback tokens issued by :class:`CallbackStateStore`."""

    home: str
    back: str
    refresh: str
    previous: str
    next: str


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[str, ...]
    index: int
    count: int


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


def paginate(items: tuple[str, ...], *, requested_page: int, page_size: int) -> Page:
    """Select a deterministic, clamped page from an already ordered collection."""

    if page_size < 1:
        raise ValueError("page size must be positive")
    if not items:
        return Page(items=(), index=0, count=0)

    count = ceil(len(items) / page_size)
    index = min(max(requested_page, 0), count - 1)
    start = index * page_size
    return Page(items=tuple(items[start : start + page_size]), index=index, count=count)


def render_empty(subject: str, callbacks: NavigationCallbacks) -> RenderedMessage:
    """Render a concise empty state without carrying application details into callbacks."""

    prefix = "No "
    suffix = " available."
    subject_limit = MAX_TELEGRAM_TEXT_UNITS - _utf16_units(prefix + suffix)
    return _message(
        prefix + _bounded_escaped(subject, subject_limit) + suffix,
        _navigation(callbacks, include_back=False),
    )


def render_degraded(callbacks: NavigationCallbacks) -> RenderedMessage:
    """Render a safe degraded state without exposing an application error or path."""

    return _message(
        "The service is temporarily unavailable.\nRefresh to try again.",
        _navigation(callbacks, include_back=False),
    )


def render_paginated(
    title: str,
    page: Page,
    callbacks: NavigationCallbacks,
) -> RenderedMessage:
    """Render a page from the supplied snapshot; callers re-resolve state on callback use."""

    if not page.items:
        return render_empty(title.lower(), callbacks)

    page_line = f"\nPage {page.index + 1} of {page.count}"
    title_limit = MAX_TELEGRAM_TEXT_UNITS - _utf16_units("<b></b>" + page_line)
    text = f"<b>{_bounded_escaped(title, title_limit)}</b>{page_line}"
    for item in page.items:
        remaining = MAX_TELEGRAM_TEXT_UNITS - _utf16_units(text + "\n")
        if remaining < 1:
            break
        text += "\n" + _bounded_escaped(item, remaining)
        if _utf16_units(text) == MAX_TELEGRAM_TEXT_UNITS:
            break
    keyboard = _navigation(
        callbacks,
        include_back=True,
        include_previous=page.index > 0,
        include_next=page.index < page.count - 1,
    )
    return _message(text, keyboard)


def _message(text: str, keyboard: tuple[tuple[Button, ...], ...]) -> RenderedMessage:
    # The last gate every screen passes, and the one that makes the guarantee hold for text
    # this module did not compose: `service` builds most of its screens with f-strings around
    # `escape(...)`, which is HTML-safe but says nothing about what UTF-16 can carry.
    text = encodable_text(text)
    if _utf16_units(text) > MAX_TELEGRAM_TEXT_UNITS:
        raise ValueError("presenter text exceeds the Telegram message limit")
    return RenderedMessage(text=text, keyboard=keyboard)


def _navigation(
    callbacks: NavigationCallbacks,
    *,
    include_back: bool,
    include_previous: bool = False,
    include_next: bool = False,
) -> tuple[tuple[Button, ...], ...]:
    _validate_callbacks(callbacks)
    rows: list[tuple[Button, ...]] = []
    if include_back:
        rows.append((Button("Back", callbacks.back),))
    if include_previous or include_next:
        pagination: list[Button] = []
        if include_previous:
            pagination.append(Button("Previous", callbacks.previous))
        if include_next:
            pagination.append(Button("Next", callbacks.next))
        rows.append(tuple(pagination))
    rows.append((Button("Refresh", callbacks.refresh), Button("Home", callbacks.home)))
    return tuple(rows)


def _validate_callbacks(callbacks: NavigationCallbacks) -> None:
    for callback in (
        callbacks.home,
        callbacks.back,
        callbacks.refresh,
        callbacks.previous,
        callbacks.next,
    ):
        _validate_callback(callback)


def _validate_callback(callback: str) -> None:
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
