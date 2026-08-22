"""Both surfaces render a capture through the one function, each with its own numbers.

`application/captures.render_capture` is deliberately unopinionated about how much output a
surface shows: the bounds are the merge this sub-plan's Research Summary excludes, because
unifying them would show one surface four times more output than it shows today, which is a
functionality change wearing a refactor's clothes. What follows is therefore not a test of the
shared function — it is a test of the two call sites, driving each surface's real entry point
with the shared renderer replaced by a recorder, and reading the arguments that arrive.

The refusal is checked from the same seam and for the same reason: the shared renderer only
signals that a capture was binary, so each surface's sentence is the surface's own, and the
two sentences differ. A merge that let either one drift to the other's wording would leave
every behavioural test in both adapter suites green.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from remote_agents.adapters.telegram import inspection
from remote_agents.adapters.telegram.inspection import inspect_capture
from remote_agents.adapters.tui.screens import sessions
from remote_agents.application.captures import RenderedCapture

#: The bounds each surface is expected to hand the shared renderer, restated here rather than
#: imported from the modules under test. Importing them would make this file agree with
#: whatever those modules currently say, which is the one thing it must not do: its whole job
#: is to fail when a call site's numbers move.
_BOT_MAX_LINES = 500
_BOT_MAX_BYTES = 128 * 1024
_PANE_MAX_LINES = 2000
_PANE_MAX_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class _Call:
    raw: bytes
    max_lines: int
    max_bytes: int
    redactions: tuple[str, ...]


def _recording_into(calls: list[_Call]):
    def render(
        raw: bytes,
        *,
        max_lines: int,
        max_bytes: int,
        redactions: tuple[str, ...] = (),
    ) -> RenderedCapture:
        calls.append(_Call(raw, max_lines, max_bytes, redactions))
        return RenderedCapture("recorded output", False)

    return render


def test_the_bot_passes_its_own_bounds_to_the_shared_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[_Call] = []
    monkeypatch.setattr(inspection, "render_capture", _recording_into(calls))

    inspect_capture(b"agent output", redactions=("secret",))

    assert calls == [_Call(b"agent output", _BOT_MAX_LINES, _BOT_MAX_BYTES, ("secret",))]


def test_the_pane_passes_its_own_bounds_to_the_shared_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[_Call] = []
    monkeypatch.setattr(sessions, "render_capture", _recording_into(calls))

    sessions.capture_for_pane("agent output", ("secret",))

    assert calls == [_Call(b"agent output", _PANE_MAX_LINES, _PANE_MAX_BYTES, ("secret",))]


def test_the_two_surfaces_do_not_share_a_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stated as its own claim, so unifying the numbers cannot pass by editing one constant."""
    calls: list[_Call] = []
    monkeypatch.setattr(inspection, "render_capture", _recording_into(calls))
    monkeypatch.setattr(sessions, "render_capture", _recording_into(calls))

    inspect_capture(b"agent output")
    sessions.capture_for_pane("agent output", ())

    assert calls[0].max_lines != calls[1].max_lines
    assert calls[0].max_bytes != calls[1].max_bytes


def test_each_surface_still_words_the_binary_refusal_for_itself() -> None:
    bot = inspect_capture(b"safe\x00binary")
    pane = sessions.capture_for_pane("safe\x00binary", ())

    assert bot.kind == "refused"
    assert bot.text == "binary output cannot be displayed."
    assert bot.attachment is None
    assert pane == "This session's output is binary and cannot be displayed."


def test_the_bot_keeps_its_truncation_notice_and_the_pane_never_grows_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `truncated` flag is the bot's alone; the pane scrolls and says nothing about it."""
    monkeypatch.setattr(
        inspection,
        "render_capture",
        lambda raw, **_: RenderedCapture("recorded output", True),
    )
    monkeypatch.setattr(
        sessions,
        "render_capture",
        lambda raw, **_: RenderedCapture("recorded output", True),
    )

    bot = inspect_capture(b"agent output")
    pane = sessions.capture_for_pane("agent output", ())

    assert bot.truncated is True
    assert bot.text == "recorded output\n[output truncated]"
    assert pane == "recorded output"
