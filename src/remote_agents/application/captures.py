"""The one bounded rendering of a captured pane, for both frontends.

Showing an owner what an agent has printed was written twice — `adapters/telegram/inspection.py`
and `adapters/tui/screens/sessions.py: render_capture` — as the same three steps: refuse a
capture holding NUL, measure whether the capture ran past the surface's bounds, hand the bytes
to `ports/terminal_text.sanitize_terminal_text`. Two copies is the arrangement this module
retires (ARCH-B4).

**The bounds are not unified, and that is deliberate.** The bot shows 500 lines or 128 KiB and
the pane shows 2000 or 512 KiB, because a Telegram message is bounded and a scrollable local
pane is not. No decision entry sets either pair — they exist only in the two adapters — so
choosing one here would mean one surface silently showing four times more or four times less
output than it shows today. That is a behaviour change, not a merge, so `max_lines` and
`max_bytes` arrive from the caller and this module has no default for either.

**Refusal is signalled, not worded.** Both surfaces refuse a capture containing NUL for the
same reason — a pane emitting NUL is not rendering text, and printing it to a terminal can
corrupt the display — but they say so in different sentences, sized and phrased for a chat
message and for a full-screen pane. A shared renderer that returned a sentence would have to
pick one of them, silently rewording the other surface. So `text is None` is the signal and
each caller writes its own refusal from it.

**It stops short of each surface's presentation boundary** (DEC-014). What comes back is
bounded, control-filtered, redacted text and nothing more: the bot's inline size cap, its file
fallback and the pass that makes text encodable are all still applied by the adapters, at their
own boundaries, on this function's output. Nothing here should be read as having done them.
"""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.ports.terminal_text import sanitize_terminal_text


@dataclass(frozen=True, slots=True)
class RenderedCapture:
    """What a surface may show, and whether the capture ran past what it may show.

    `text is None` is the refusal: the capture was not text and no surface should print it.
    It is `None` rather than `""` so that a caller cannot render the refusal by forgetting to
    check for it — an empty capture and a refused one are different sentences on both surfaces.
    """

    text: str | None
    truncated: bool


def render_capture(
    raw: bytes,
    *,
    max_lines: int,
    max_bytes: int,
    redactions: tuple[str, ...] = (),
) -> RenderedCapture:
    """Bound and sanitize a raw pane capture, or signal that it was not text."""

    if b"\x00" in raw:
        return RenderedCapture(None, False)
    # Measured on the raw bytes against the same bounds the sanitizer is about to apply, which
    # is not the same question as how many lines survived it: `sanitize_terminal_text` strips
    # the trailing newline, so a capture that ends in one and exactly fills the line bound
    # would look untruncated after the fact. The bot's truncation notice hangs off this flag.
    truncated = len(raw) > max_bytes or raw.count(b"\n") + 1 > max_lines
    text = sanitize_terminal_text(
        raw,
        max_lines=max_lines,
        max_bytes=max_bytes,
        redactions=redactions,
    )
    return RenderedCapture(text, truncated)
