"""Technology-neutral safety transformation for bounded terminal text."""

from __future__ import annotations

import re

_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


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
    text = "".join(character for character in text if character == "\n" or character >= " ")
    for pattern in redactions:
        if pattern:
            text = text.replace(pattern, "[REDACTED]")
    return "\n".join(text.splitlines()[:max_lines]).strip()
