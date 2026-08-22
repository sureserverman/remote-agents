"""One bounded capture rendering, driven the way both frontends drive it.

The NUL refusal and the `sanitize_terminal_text` call existed twice — `telegram/inspection.py`
and `tui/screens/sessions.py: render_capture` — **with different bounds**: 500 lines / 128 KiB
on the bot, 2000 / 512 KiB in the pane. Those bounds are the one part of the duplication this
merge must not resolve, so they arrive as arguments and nothing in this file asserts a value
for them; the two call sites' own numbers are pinned where the callers are, in
`tests/unit/adapters/test_shared_capture_rendering.py`.

**Refusal is signalled, not worded.** The two surfaces refuse a binary capture in different
sentences — "binary output cannot be displayed." on the bot, "This session's output is binary
and cannot be displayed." in the pane — and a shared renderer that picked one of them would
silently reword the other surface. `text is None` is the whole signal, and each caller writes
its own sentence from it. That is asserted twice over: behaviourally, and against the module's
own source, because a renderer can signal correctly today and still be the obvious place for
somebody to add a default sentence tomorrow.
"""

from __future__ import annotations

import pathlib

import pytest

from remote_agents.application.captures import RenderedCapture, render_capture

_SOURCE = (
    pathlib.Path(__file__).resolve().parents[3]
    / "src"
    / "remote_agents"
    / "application"
    / "captures.py"
).read_text(encoding="utf-8")

_BOUNDS = {"max_lines": 500, "max_bytes": 128 * 1024}


def test_a_capture_holding_nul_is_refused_with_no_text_at_all() -> None:
    rendered = render_capture(b"safe\x00binary", **_BOUNDS)

    assert rendered == RenderedCapture(None, False)


def test_neither_surfaces_refusal_sentence_lives_in_the_shared_module() -> None:
    """The wording stays with the surface that speaks it.

    Source text rather than behaviour, because the behavioural test above passes for a
    renderer that carries an unused default sentence, and an unused default sentence is how
    one surface starts emitting the other's words.
    """
    assert "binary output cannot be displayed" not in _SOURCE
    assert "This session's output is binary" not in _SOURCE


def test_the_line_bound_is_the_callers_and_not_a_default() -> None:
    rendered = render_capture(b"one\ntwo\nthree", max_lines=2, max_bytes=64 * 1024)

    assert rendered.text == "one\ntwo"


def test_the_byte_bound_is_the_callers_and_bites_before_the_decode() -> None:
    rendered = render_capture(b"abcdefgh", max_lines=10, max_bytes=4)

    assert rendered.text == "abcd"


def test_truncation_is_measured_on_the_raw_bytes_and_not_on_the_text_that_survived() -> None:
    """`raw.count(b"\\n") + 1 > max_lines`, which is not `len(text.splitlines()) > max_lines`.

    The bot's `[output truncated]` notice and its attachment path both hang off this flag, and
    the two measurements disagree on exactly the input that matters: two lines with a trailing
    newline against a bound of two. The sanitizer strips that newline, so a post-sanitize count
    says "nothing was dropped" — while the raw count says the capture ran past the bound, which
    is what the owner is being told.
    """
    rendered = render_capture(b"one\ntwo\n", max_lines=2, max_bytes=64 * 1024)

    assert rendered.text == "one\ntwo"
    assert rendered.truncated is True


def test_a_capture_inside_both_bounds_is_not_truncated() -> None:
    rendered = render_capture(b"one\ntwo", max_lines=2, max_bytes=64 * 1024)

    assert rendered.truncated is False


def test_a_capture_past_the_byte_bound_is_truncated() -> None:
    rendered = render_capture(b"x" * 10, max_lines=10, max_bytes=4)

    assert rendered.truncated is True


def test_the_shared_sanitizer_still_does_the_filtering_and_the_redacting() -> None:
    """Nothing is re-implemented here — control sequences, tabs and secrets are the port's."""
    rendered = render_capture(
        b"\x1b[31mred\x1b[0m\tsecret", max_lines=10, max_bytes=1024, redactions=("secret",)
    )

    assert rendered.text == "red     [REDACTED]"


def test_a_non_positive_bound_is_refused_by_the_shared_sanitizer() -> None:
    with pytest.raises(ValueError):
        render_capture(b"anything", max_lines=0, max_bytes=1024)


def test_the_shared_renderer_does_not_absorb_telegrams_presentation_wrapper() -> None:
    """DEC-014 puts the encodability pass at each surface's own presentation boundary.

    This renderer sits upstream of both, so Telegram's 4096-UTF-16-unit inline cap, its
    `session-output.txt` fallback and its `encodable_text` pass stay in `inspection.py` and
    `presenters.py`. A reader must not be able to assume this function inherited any of them.
    """
    assert "utf-16" not in _SOURCE.casefold()
    assert "4096" not in _SOURCE
    assert "session-output.txt" not in _SOURCE
    assert "encodable_text" not in _SOURCE
