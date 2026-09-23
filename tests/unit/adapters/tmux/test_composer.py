"""The pane-state classifier, against the real captures of `docs/acceptance-2026-09-22-...`.

Every file under `tests/fixtures/panes/<agent>/` was taken from a real pane on the installed build
(Task 1.2 of the prompt-relay plan), and its name says what the screen was showing. The relay types
into a pane only on IDLE, so the property that matters most is one-sided: **no capture that is not
an idle composer may classify as IDLE.** The rest -- that each idle capture does classify as IDLE,
each dialog as DIALOG -- is what makes the relay useful rather than merely safe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import provider_descriptors
from remote_agents.adapters.tmux.composer import PaneState, classify, composer_draft

_PANES = Path(__file__).resolve().parents[3] / "fixtures" / "panes"

#: The fixture directory is named for the agent; the descriptor for its profile.
_PROFILE = {"claude": "claude", "codex": "codex", "opencode": "opencode", "cursor": "cursor-agent"}

#: Captures whose composer is buried under a command menu drawn below it. They are not idle, and
#: the classifier cannot read a draft out of them either, so UNKNOWN is the honest answer.
_MENU_OVER_COMPOSER = {"codex/composed_slash", "cursor/composed_space_slash"}


def _descriptor(agent: str):
    return next(
        descriptor
        for descriptor in provider_descriptors()
        if str(descriptor.profile_id) == _PROFILE[agent]
    )


def _captures() -> list[tuple[str, str]]:
    return sorted(
        (path.parent.name, path.stem)
        for path in _PANES.glob("*/*.txt")
        if path.parent.name in _PROFILE
    )


def _expected(agent: str, name: str) -> PaneState:
    if f"{agent}/{name}" in _MENU_OVER_COMPOSER:
        return PaneState.UNKNOWN
    for prefix, state in (
        ("idle", PaneState.IDLE),
        ("busy", PaneState.BUSY),
        ("dialog", PaneState.DIALOG),
        ("composed", PaneState.COMPOSING),
    ):
        if name.startswith(prefix):
            return state
    raise AssertionError(f"{agent}/{name}.txt does not say what it shows")


def test_the_captures_are_there_to_be_classified() -> None:
    """Vacuity guard: a glob over an empty directory would pass every case below."""
    agents = {agent for agent, _name in _captures()}
    assert agents == set(_PROFILE), agents
    assert len(_captures()) >= 40


@pytest.mark.parametrize(("agent", "name"), _captures(), ids=lambda value: value)
def test_every_capture_classifies_as_its_name_says(agent: str, name: str) -> None:
    screen = (_PANES / agent / f"{name}.txt").read_text(encoding="utf-8")

    assert classify(screen, _descriptor(agent)) is _expected(agent, name)


@pytest.mark.parametrize(("agent", "name"), _captures(), ids=lambda value: value)
def test_no_capture_of_another_agent_reads_as_idle(agent: str, name: str) -> None:
    """A declaration matched against the wrong agent's screen must not find an idle composer."""
    screen = (_PANES / agent / f"{name}.txt").read_text(encoding="utf-8")
    for other in _PROFILE:
        if other != agent:
            assert classify(screen, _descriptor(other)) is not PaneState.IDLE, other


@pytest.mark.parametrize("agent", sorted(_PROFILE))
@pytest.mark.parametrize(
    "screen",
    ["", "\n\n\n", "$ ls\nREADME.md  src\n$ ", "Connection to host closed.\n"],
    ids=["blank", "newlines", "a-shell", "unrelated"],
)
def test_a_screen_that_is_not_the_agent_is_unknown(agent: str, screen: str) -> None:
    assert classify(screen, _descriptor(agent)) is PaneState.UNKNOWN


@pytest.mark.parametrize("agent", sorted(_PROFILE))
def test_a_truncated_capture_is_not_idle(agent: str) -> None:
    """The top half of an idle screen, cut before its composer, says nothing about the composer."""
    lines = (_PANES / agent / "idle.txt").read_text(encoding="utf-8").rstrip("\n").splitlines()
    top = "\n".join(lines[: len(lines) // 2])

    assert classify(top, _descriptor(agent)) is not PaneState.IDLE


@pytest.mark.parametrize("agent", sorted(_PROFILE))
def test_a_dialog_wins_over_an_idle_composer_on_the_same_screen(agent: str) -> None:
    """An idle composer with any of this agent's dialogs drawn above it is DIALOG, never IDLE."""
    descriptor = _descriptor(agent)
    idle = (_PANES / agent / "idle.txt").read_text(encoding="utf-8")
    dialogs = sorted((_PANES / agent).glob("dialog_*.txt"))
    assert dialogs, f"no dialog captured for {agent}"

    for path in dialogs:
        dialog = path.read_text(encoding="utf-8")
        assert classify(f"{dialog}\n{idle}", descriptor) is PaneState.DIALOG, path.name


def test_a_descriptor_without_a_composer_is_unknown() -> None:
    from dataclasses import replace

    descriptor = replace(_descriptor("claude"), composer=None)
    idle = (_PANES / "claude" / "idle.txt").read_text(encoding="utf-8")

    assert classify(idle, descriptor) is PaneState.UNKNOWN


@pytest.mark.parametrize(
    ("agent", "draft"),
    [
        ("claude", "Count from 1 to 60, one number per line, then say the word pong.\n"
                   "This second line mentions Enter and C-c as plain words."),
        ("codex", "Write about 300 words on why terminal multiplexers are useful.\n"
                  "Second line, plain words: Enter and C-c."),
        ("opencode", "Write about 300 words on why terminal multiplexers are useful.\n"
                     "Second line, plain words: Enter and C-c."),
        ("cursor", "Write about 300 words on why terminal multiplexers are useful.\n"
                   "Second line, plain words: Enter and C-c."),
    ],
)  # fmt: skip
def test_the_draft_is_read_back_line_for_line(agent: str, draft: str) -> None:
    """What `send_prompt` compares its paste against before it may press Enter."""
    screen = (_PANES / agent / "composed.txt").read_text(encoding="utf-8")

    assert composer_draft(screen, _descriptor(agent)) == draft


@pytest.mark.parametrize("agent", sorted(_PROFILE))
def test_an_idle_composer_reads_back_an_empty_draft(agent: str) -> None:
    screen = (_PANES / agent / "idle.txt").read_text(encoding="utf-8")

    assert composer_draft(screen, _descriptor(agent)) == ""


@pytest.mark.parametrize(
    ("agent", "folded"),
    [("claude", "[Pasted text #1]"), ("codex", "[Pasted Content 2969 chars]")],
)
def test_a_folded_long_paste_is_read_back_as_its_placeholder(agent: str, folded: str) -> None:
    screen = (_PANES / agent / "composed_long.txt").read_text(encoding="utf-8")

    assert composer_draft(screen, _descriptor(agent)) == folded
