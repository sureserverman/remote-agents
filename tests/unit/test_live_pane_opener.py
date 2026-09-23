"""The live drills' pane opener, fed scripted screens instead of an agent (BL-105).

`tests/support/agent_panes.py` opens real agent panes for the live drills. These cases need no
agent, so they live here, where every suite collects them: the one property the live drills
cannot show on a good day -- that a screen the drill does not recognise FAILS, with the pane in
the message, instead of skipping -- must not depend on someone running `tests/live` by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_panes import (
    CLAUDE_OPENING,
    CLAUDE_READY,
    CODEX_OPENING,
    CODEX_READY,
    open_to_composer,
)

_PANES = Path(__file__).resolve().parents[1] / "fixtures" / "panes"


class _Screens:
    """A pane that shows each screen in turn, advancing when a key is pressed."""

    def __init__(self, *screens: str) -> None:
        self.screens = list(screens)
        self.pressed: list[str] = []

    def capture(self) -> str:
        return self.screens[0]

    def press(self, key: str) -> None:
        self.pressed.append(key)
        if key == "Enter" and len(self.screens) > 1:
            self.screens.pop(0)


def _fake_clock():
    now = [0.0]
    return (lambda: now[0]), (lambda seconds: now.__setitem__(0, now[0] + seconds))


def test_the_opener_fails_on_a_screen_it_does_not_recognise_and_shows_it() -> None:
    clock, sleep = _fake_clock()
    screens = _Screens("Something new: pick a plan\n› 1. Pro\n  2. Free")

    # BaseException, then the type: a skip is also an outcome exception, and a skip here is
    # the exact defect this pins -- `pytest.raises(pytest.fail.Exception)` would let it through.
    with pytest.raises(BaseException) as failure:
        open_to_composer(
            screens.capture, screens.press, ready=CODEX_READY, interstitials=CODEX_OPENING,
            agent="codex", timeout=10, clock=clock, sleep=sleep,
        )  # fmt: skip

    assert failure.type is pytest.fail.Exception, f"it must FAIL, not {failure.type.__name__}"
    assert "Something new: pick a plan" in str(failure.value), "the pane must be in the failure"
    assert screens.pressed == [], "nothing may be typed into a screen the drill does not know"


def test_the_opener_answers_codex_0_155_s_rate_limit_prompt_by_its_words() -> None:
    """BL-105's screen: the option is chosen by name, wherever the highlight starts."""
    clock, sleep = _fake_clock()
    screens = _Screens(
        "Do you trust the contents of this directory?\n› 1. Yes, continue\n  2. No, quit",
        "Approaching rate limits\n› 1. Switch to gpt-6-mini\n  2. Keep current model\n"
        "  3. Keep current model (never show again)",
        "Hooks need review\n  1. Review\n› 2. Trust all and continue\n  3. Continue without",
        f"› {CODEX_READY}",
    )

    open_to_composer(
        screens.capture, screens.press, ready=CODEX_READY, interstitials=CODEX_OPENING,
        agent="codex", timeout=30, clock=clock, sleep=sleep,
    )  # fmt: skip

    assert screens.pressed == ["Enter", "Down", "Enter", "Enter"]


def test_the_opener_answers_claude_s_unnumbered_trust_prompt_from_a_real_capture() -> None:
    """Claude 2.1.280 lists `No, exit` first and numbers nothing; the choice is by words."""
    clock, sleep = _fake_clock()
    trust = (
        Path(__file__).resolve().parents[1] / "fixtures" / "panes" / "claude" / "dialog_trust.txt"
    ).read_text(encoding="utf-8")
    screens = _Screens(trust, f"  ⏵⏵ {CLAUDE_READY} on (shift+tab to cycle)")

    open_to_composer(
        screens.capture, screens.press, ready=CLAUDE_READY, interstitials=CLAUDE_OPENING,
        agent="claude", timeout=30, clock=clock, sleep=sleep,
    )  # fmt: skip

    assert screens.pressed == ["Down", "Enter"], "`Yes, I trust this folder` is the second line"


def test_the_opener_waits_for_a_menu_drawn_a_frame_after_its_marker() -> None:
    clock, sleep = _fake_clock()
    half = "Do you trust the contents of this directory?"
    screens = _Screens(half, f"{half}\n› 1. Yes, continue\n  2. No, quit", f"› {CODEX_READY}")
    original = screens.capture
    calls = []

    def capture() -> str:
        calls.append(1)
        if len(calls) == 2:
            screens.screens.pop(0)  # the menu finishes drawing on the second look
        return original()

    open_to_composer(
        capture, screens.press, ready=CODEX_READY, interstitials=CODEX_OPENING,
        agent="codex", timeout=30, clock=clock, sleep=sleep,
    )  # fmt: skip

    assert screens.pressed == ["Enter"]


def test_the_opener_fails_on_a_known_prompt_whose_option_never_appears() -> None:
    clock, sleep = _fake_clock()
    screens = _Screens("Approaching rate limits\n› 1. Switch to gpt-6-mini\n  2. Upgrade")

    with pytest.raises(BaseException) as failure:
        open_to_composer(
            screens.capture, screens.press, ready=CODEX_READY, interstitials=CODEX_OPENING,
            agent="codex", timeout=10, clock=clock, sleep=sleep,
        )  # fmt: skip

    assert failure.type is pytest.fail.Exception
    assert "Keep current model" in str(failure.value) and "Upgrade" in str(failure.value)
    assert screens.pressed == []


def test_the_highlight_is_the_last_glyph_line_not_a_transcript_echo() -> None:
    """Claude echoes each prompt as `❯ <text>` above a live `❯ 1. Yes` menu (a real capture)."""
    from agent_panes import _select

    approval = (_PANES / "claude" / "dialog_approval.txt").read_text(encoding="utf-8")
    assert approval.count("❯ ") > 1, "the premise: the transcript carries the glyph too"
    pressed: list[str] = []

    assert _select(approval, "No", pressed.append)
    assert pressed == ["Down", "Down", "Down", "Enter"], "`4. No` is three below `1. Yes`"
