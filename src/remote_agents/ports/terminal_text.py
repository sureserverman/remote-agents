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
