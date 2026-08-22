"""Bounding and sanitizing a capture is asked of the application, never restated by a frontend.

The fifth and last of this stage's merges to get a shape guard, and it got one for the reason
the other four did: `tests/unit/adapters/test_shared_capture_rendering.py` pins both call
sites' *arguments* and both refusal *sentences*, which is what that file is for, and neither
would notice a frontend that re-implemented the transformation under a fresh name beside them.
A Tier-2 review and an independent evaluator arrived at that gap separately, which is usually
the sign it is real.

**The shape is "NUL guard plus sanitizer", because that is what the duplicate was.** Both
surfaces held the same three steps — refuse a capture containing NUL, measure it against the
surface's bounds, hand the bytes to `ports/terminal_text.sanitize_terminal_text` — and the two
halves worth sweeping are the ones a re-derivation cannot avoid: it must reach the sanitizer,
and it must test the bytes for NUL.

**What the sweep deliberately allows.** The TUI uses `\\x00` inside *string* sentinels
(`NEVER_EMPTY`, the `\\x00back` option keys), which is an unrelated use of the same escape and
predates all of this — so the sweep looks for a `bytes` constant containing NUL, not for the
escape in source text. `adapters/tmux/capture.py` is outside both frontend trees and stays
there: capturing a pane is that adapter's job, and widening this guard to reach it would make
it mean something else.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[3] / "src" / "remote_agents"
_FRONTENDS = {"telegram": _SRC / "adapters" / "telegram", "tui": _SRC / "adapters" / "tui"}


def _sources(tree_root: pathlib.Path) -> list[pathlib.Path]:
    assert tree_root.is_dir(), f"{tree_root} must exist or this sweep passes over nothing"
    return sorted(tree_root.rglob("*.py"))


def _nodes(tree_root: pathlib.Path):
    for path in _sources(tree_root):
        for node in ast.walk(ast.parse(path.read_text())):
            yield path, node


@pytest.mark.parametrize("frontend", sorted(_FRONTENDS))
def test_no_frontend_reaches_the_sanitizer_itself(frontend: str) -> None:
    """The bounded transformation is `application/captures.render_capture`, and it is asked.

    A frontend importing or calling `sanitize_terminal_text` is applying the bounds itself,
    which is the arrangement that let the two surfaces hold their own copies of the same three
    steps — and, because each passes its own numbers, the arrangement in which one of them
    could quietly start passing the other's.
    """
    offenders = [
        f"{path.relative_to(_SRC.parent.parent)}:{node.lineno}"
        for path, node in _nodes(_FRONTENDS[frontend])
        if (
            isinstance(node, ast.Name | ast.Attribute)
            and "sanitize_terminal_text" in {getattr(node, "id", None), getattr(node, "attr", None)}
        )
        or (isinstance(node, ast.alias) and node.name == "sanitize_terminal_text")
    ]

    assert offenders == [], f"{frontend} bounds a capture itself rather than asking: {offenders}"


@pytest.mark.parametrize("frontend", sorted(_FRONTENDS))
def test_no_frontend_tests_captured_bytes_for_nul_itself(frontend: str) -> None:
    """The refusal *decision* is the shared renderer's; only the *sentence* is the surface's.

    A `bytes` literal containing NUL in a frontend is the other half of the re-derivation. The
    TUI's `\\x00` string sentinels are a different thing and are not matched: this looks at the
    constant's type, not at the source text.
    """
    offenders = [
        f"{path.relative_to(_SRC.parent.parent)}:{node.lineno}"
        for path, node in _nodes(_FRONTENDS[frontend])
        if isinstance(node, ast.Constant)
        and isinstance(node.value, bytes)
        and b"\x00" in node.value
    ]

    assert offenders == [], f"{frontend} decides the binary refusal itself: {offenders}"


@pytest.mark.parametrize("frontend", sorted(_FRONTENDS))
def test_each_frontend_still_asks_for_a_bounded_rendering(frontend: str) -> None:
    """The complement, so the two exact-zero sweeps cannot pass by nobody rendering a capture."""
    reaches = [
        path.name
        for path, node in _nodes(_FRONTENDS[frontend])
        if isinstance(node, ast.Name | ast.Attribute)
        and "render_capture" in {getattr(node, "id", None), getattr(node, "attr", None)}
    ]

    assert reaches, f"{frontend} no longer renders a capture through the shared function"
