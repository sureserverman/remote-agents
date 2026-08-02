"""Pane-output safety boundary: bounded text only, never durable storage."""

from __future__ import annotations

from remote_agents.ports.terminal_text import sanitize_terminal_text


def sanitize_capture(
    raw: bytes,
    *,
    max_lines: int,
    max_bytes: int,
    redactions: tuple[str, ...] = (),
) -> str:
    """Return bounded UTF-8 pane text with ANSI, unsafe controls, and secrets removed."""
    return sanitize_terminal_text(
        raw,
        max_lines=max_lines,
        max_bytes=max_bytes,
        redactions=redactions,
    )
