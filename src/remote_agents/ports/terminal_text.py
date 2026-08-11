"""Technology-neutral safety transformation for bounded terminal text."""

from __future__ import annotations

import re

_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")

#: The terminal default both surfaces are read in.
_TAB_WIDTH = 8


def sanitize_terminal_text(
    raw: bytes,
    *,
    max_lines: int,
    max_bytes: int,
    redactions: tuple[str, ...] = (),
) -> str:
    """Return bounded UTF-8 text with unsafe controls and configured secrets removed."""
    if max_lines < 1 or max_bytes < 1:
        raise ValueError("capture limits must be positive")
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    text = _ANSI_ESCAPE.sub("", text)
    # Tabs become spaces *before* the control filter, rather than being deleted by it. The
    # filter keeps only `\n` and anything at or above `" "`, and `\t` is 0x09 — so a tab was
    # dropped with nothing in its place and tab-separated agent output arrived as joined
    # words: `col1\tcol2` rendered as `col1col2`. Neither surface could tell that had
    # happened, because the damage is indistinguishable from output that never had columns.
    #
    # Expanded rather than exempted. Passing `\t` through the filter would put a real control
    # character into a string this function exists to promise has none, and both consumers
    # render it into a terminal. A fixed eight-column stop is the ordinary terminal default;
    # `expandtabs` computes it per line, which is what keeps a column that follows a wide
    # prefix from collapsing.
    text = text.expandtabs(_TAB_WIDTH)
    text = "".join(character for character in text if character == "\n" or character >= " ")
    for pattern in redactions:
        if pattern:
            text = text.replace(pattern, "[REDACTED]")
    return "\n".join(text.splitlines()[:max_lines]).strip()


def encodable_text(text: str) -> str:
    """Replace every code point no encoder will carry, so downstream text handling is total.

    Here, beside `sanitize_terminal_text`, because it answers the same kind of question about a
    different input. That one takes bytes this project decoded itself and is safe by
    construction; this one takes a `str` that arrived **already decoded by somebody else**, and
    such a string can hold a lone surrogate — legal in memory, refused by every encoder.

    Two producers do exactly that today, and neither is exotic:

    - `json.loads` turns a `\\udXXX` escape in a spooled hook payload back into a lone
      surrogate, and the spool tolerates a foreign writer by design;
    - `os.listdir` decodes an undecodable filename with `surrogateescape`, so a project
      directory whose name is not valid UTF-8 carries one into the project list.

    Both reached a UTF-16 budget calculation and raised `UnicodeEncodeError` out of the render.
    U+FFFD is the replacement, matching what `sanitize_terminal_text`'s decode already produces,
    so one marker means "this was not text" wherever the reader meets it.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return "".join(character if _encodable(character) else "�" for character in text)
    return text


def _encodable(character: str) -> bool:
    try:
        character.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
