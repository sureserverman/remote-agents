"""Read-only, bounded terminal-output presentation for Telegram."""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.application.captures import render_capture


@dataclass(frozen=True, slots=True)
class InspectionResult:
    kind: str
    text: str
    attachment: bytes | None
    filename: str | None
    redacted: bool
    truncated: bool


def inspect_capture(
    raw: bytes,
    *,
    redactions: tuple[str, ...] = (),
    telegram_limit: int = 4096,
) -> InspectionResult:
    """Present safe captured output without accepting or sending agent input."""

    if telegram_limit < 1:
        raise ValueError("Telegram text limit must be positive")
    rendered = render_capture(raw, max_lines=500, max_bytes=128 * 1024, redactions=redactions)
    if rendered.text is None:
        return InspectionResult(
            "refused", "binary output cannot be displayed.", None, None, False, False
        )
    text = rendered.text
    truncated = rendered.truncated
    redacted = "[REDACTED]" in text
    inline_text = text + ("\n[output truncated]" if truncated else "")
    if len(inline_text.encode("utf-16-le")) // 2 <= telegram_limit:
        return InspectionResult(
            "text",
            inline_text,
            None,
            None,
            redacted,
            truncated,
        )
    return InspectionResult(
        "attachment",
        "Output is attached as UTF-8 text." + (" Output was truncated." if truncated else ""),
        text.encode(),
        "session-output.txt",
        redacted,
        truncated,
    )
