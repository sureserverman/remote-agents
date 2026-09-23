"""Open a real agent TUI to its composer by answering what is on screen, never by a script.

The live drills used to follow a fixed opening: wait for the trust prompt, answer it, wait for
the hook prompt, answer it, wait for the composer. Codex 0.155.1 put a rate-limit prompt in the
middle of that on some accounts, the wait for the composer timed out, and the drill *skipped* --
a skip and a genuine break are the same line in a summary (BL-105).

So the opener polls the pane and answers whichever known interstitial is showing, choosing the
option by its words rather than by counting key presses from a remembered position, until the
composer appears. A screen it cannot recognise is a **failure carrying the pane's text**: it
means the drill has stopped testing, which is not a property of the host. Skips stay for the
host's own lacks -- a missing binary, missing credentials, an unset flag -- and are decided
before a pane is opened, not here.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import pytest


@dataclass(frozen=True, slots=True)
class Interstitial:
    """A screen an agent can raise before its composer, and the option that gets past it."""

    marker: str
    """Text that is on the screen only while this prompt is showing."""
    choose: str
    """The words of the option to select. The first line carrying them is the one chosen."""


CODEX_OPENING = (
    Interstitial("Do you trust the contents of this directory", "Yes, continue"),
    # Hook trust, granted inside the drill's disposable CODEX_HOME only.
    Interstitial("Hooks need review", "Trust all and continue"),
    # 0.155.1, on an account near its limit (BL-105). Measured from the shipped binary's
    # strings; keep the model the drill was configured with.
    Interstitial("Approaching rate limits", "Keep current model"),
)
CODEX_READY = "Ask Codex to do anything"

CLAUDE_OPENING = (
    # Raised only for a folder claude has not been told about. The option words are the
    # 2.1.280 bundle's own ("Yes, I trust this folder" / "No, exit").
    Interstitial("Is this a project you created", "Yes, I trust this folder"),
)
CLAUDE_READY = "auto mode"

#: The glyph each TUI draws in front of the highlighted option. `>` is deliberately not one:
#: Codex opens with `> You are in <dir>`, which is a line of prose, not a highlight.
_CURSOR = re.compile(r"^(\s*[›❯]\s+)\S")


def _options(lines: list[str]) -> tuple[list[int], int] | None:
    """The option lines of the menu on screen, and which of them is highlighted.

    Options are found from the highlight outwards: every adjacent line whose text starts in the
    highlighted line's text column. The highlight is the LAST glyph line on screen. Numbered
    (Codex: `› 1. Yes, continue`) and unnumbered (Claude 2.1.280: `❯ No, exit` over
    `  Yes, I trust this folder`) menus both have that shape.
    """
    # From the bottom: the live menu is the last thing drawn, and the transcript above it can
    # carry the same glyph (Claude echoes each prompt as `❯ <text>`).
    cursor = next(
        (index for index in range(len(lines) - 1, -1, -1) if _CURSOR.match(lines[index])), None
    )
    if cursor is None:
        return None
    column = len(_CURSOR.match(lines[cursor]).group(1))

    def is_option(line: str) -> bool:
        return len(line) > column and not line[:column].strip() and line[column] != " "

    first = cursor
    while first > 0 and is_option(lines[first - 1]):
        first -= 1
    last = cursor
    while last + 1 < len(lines) and is_option(lines[last + 1]):
        last += 1
    return list(range(first, last + 1)), cursor


def _select(text: str, choose: str, press: Callable[[str], None]) -> bool:
    """Move the highlight to the option carrying `choose`, then press Enter.

    False when the screen shows no highlighted option or no such option, so the caller can
    report the screen rather than press keys into something it does not understand.
    """
    lines = text.splitlines()
    found = _options(lines)
    if found is None:
        return False
    options, cursor = found
    target = next((index for index in options if choose in lines[index]), None)
    if target is None:
        return False
    steps = options.index(target) - options.index(cursor)
    for _ in range(abs(steps)):
        press("Down" if steps > 0 else "Up")
    press("Enter")
    return True


def open_to_composer(
    capture: Callable[[], str],
    press: Callable[[str], None],
    *,
    ready: str,
    interstitials: tuple[Interstitial, ...],
    agent: str,
    timeout: float = 120.0,
    poll: float = 1.0,
    settle: float = 1.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Answer interstitials until `ready` shows; fail with the screen if it never does.

    An interstitial is checked before `ready`, because a dialog can be drawn over a composer
    whose placeholder is still on screen. Each answered prompt restarts nothing: the deadline
    is for the whole opening, so a TUI that loops on one prompt still fails in bounded time.
    """
    deadline = clock() + timeout
    text = ""
    unanswerable: Interstitial | None = None
    while clock() < deadline:
        text = capture()
        showing = next((item for item in interstitials if item.marker in text), None)
        if showing is not None:
            if _select(text, showing.choose, press):
                unanswerable = None
                sleep(settle)
            else:
                # The marker can be drawn a frame before its menu is; look again rather than
                # fail on a half-drawn screen, and report it only if it never completes.
                unanswerable = showing
                sleep(poll)
            continue
        if ready in text:
            return
        sleep(poll)
    if unanswerable is not None:
        pytest.fail(
            f"{agent} raised {unanswerable.marker!r} but no option reading "
            f"{unanswerable.choose!r} could be selected within {timeout:.0f}s. The pane:\n{text}"
        )
    pytest.fail(
        f"{agent} did not reach its composer ({ready!r}) within {timeout:.0f}s, and the drill "
        f"does not recognise what it is showing. The pane:\n{text}"
    )
