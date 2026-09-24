"""The pane-state classifier, against the real captures of `docs/acceptance-2026-09-22-...`.

Every file under `tests/fixtures/panes/<agent>/` was taken from a real pane on the installed build
(Task 1.2 of the prompt-relay plan), and its name says what the screen was showing. The relay types
into a pane only on IDLE, so the property that matters most is one-sided: **no capture that is not
an idle composer may classify as IDLE.** The rest -- that each idle capture does classify as IDLE,
each dialog as DIALOG -- is what makes the relay useful rather than merely safe.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import provider_descriptors
from remote_agents.adapters.tmux.composer import PaneState, classify, composer_draft, turn_ended

_PANES = Path(__file__).resolve().parents[3] / "fixtures" / "panes"

#: The fixture directory is named for the agent; the descriptor for its profile.
_PROFILE = {"claude": "claude", "codex": "codex", "opencode": "opencode", "cursor": "cursor-agent"}

#: Captures whose composer is buried under a command menu drawn below it. They are not idle, and
#: the classifier cannot read a draft out of them either, so UNKNOWN is the honest answer.
_MENU_OVER_COMPOSER = {"codex/composed_slash", "cursor/composed_space_slash"}

#: Shell mode (`!`) is not the prompt composer -- anything submitted there runs as a command --
#: so its composer is deliberately not matched and the screen reads UNKNOWN (Stage 2 gate).
_SHELL_MODE = {"claude/composed_shell_mode", "codex/composed_shell_mode"}


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
    if f"{agent}/{name}" in _MENU_OVER_COMPOSER | _SHELL_MODE:
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


def test_codex_is_busy_whatever_its_working_line_is_headed() -> None:
    """Codex heads the line with its reasoning summary, not always `Working`."""
    busy = (_PANES / "codex" / "busy.txt").read_text(encoding="utf-8")
    planning = busy.replace(
        "• Working (2s • esc to interrupt)", "• Planning edits (9s • esc to interrupt)"
    )
    assert planning != busy, "the busy fixture's working line moved"

    assert classify(planning, _descriptor("codex")) is PaneState.BUSY


def test_codex_is_busy_on_the_hollow_frame_of_its_spinner_too() -> None:
    """Codex 0.155.1 draws the working line's bullet as `•` and `◦` on alternate frames.

    `busy_hollow.txt` is a real `◦` frame taken mid-turn. Matching `•` alone read every other
    frame of a running turn as an idle composer, and the relay typed into it (the live drill's
    Codex cases, 2026-09-23).
    """
    hollow = (_PANES / "codex" / "busy_hollow.txt").read_text(encoding="utf-8")
    assert "◦" in hollow, "the fixture is the hollow frame"

    assert classify(hollow, _descriptor("codex")) is PaneState.BUSY


# --- a turn drawn only in the pane's title ----------------------------------------------------
#
# While Codex 0.155.1 streams its answer the screen is byte-identical to a finished turn, and only
# the title says otherwise: a braille spinner leads it for the whole turn (measured 2026-09-23).


def test_codex_with_a_spinning_title_is_busy_over_an_idle_composer() -> None:
    idle = (_PANES / "codex" / "idle.txt").read_text(encoding="utf-8")

    spinning = "⠋ Count to 150 | workspace"
    assert classify(idle, _descriptor("codex"), spinning) is PaneState.BUSY
    assert classify(idle, _descriptor("codex"), "Count to 150 | workspace") is PaneState.IDLE


def test_claude_s_title_marks_nothing_so_its_idle_composer_stays_idle() -> None:
    idle = (_PANES / "claude" / "idle.txt").read_text(encoding="utf-8")

    assert classify(idle, _descriptor("claude"), "✳ Count 1 to 150") is PaneState.IDLE


def test_a_dialog_still_wins_over_a_spinning_title() -> None:
    dialog = (_PANES / "codex" / "dialog_approval.txt").read_text(encoding="utf-8")

    spinning = "⠋ Run tests | workspace"
    assert classify(dialog, _descriptor("codex"), spinning) is PaneState.DIALOG


# --- Claude's suggested next message is not a draft -----------------------------------------


def test_claude_s_dim_suggestion_reads_as_an_idle_composer_only_when_styled() -> None:
    """After a turn Claude 2.1.280 fills its empty composer with a suggestion, drawn dim.

    As plain text it is a draft like any other, which is why every relayed message to such a
    pane was refused ("the agent's input already holds text") until the relay's captures kept
    their styling (`capture-pane -e`). The fixture is the bottom of a real owner pane.
    """
    styled = (_PANES / "claude" / "idle_suggestion.txt").read_text(encoding="utf-8")
    plain = re.sub(r"\x1b\[[0-9;:?]*[A-Za-z]", "", styled)

    assert classify(styled, _descriptor("claude")) is PaneState.IDLE
    assert classify(plain, _descriptor("claude")) is PaneState.COMPOSING


def test_a_draft_in_a_truecolour_is_still_a_draft() -> None:
    """`38;2;r;g;b` carries `2`s that are a colour model and a channel, not the dim attribute.

    Closed with a full reset, so a misread `2` would strip only the draft and leave the composer
    to be found empty -- IDLE -- rather than strip everything after it and fall back to the
    plain draft by accident, which is how the first version of this test passed a broken parse.
    """
    styled = (_PANES / "claude" / "composed_styled.txt").read_text(encoding="utf-8")
    coloured = styled.replace(
        "a draft I typed myself", "\x1b[38;2;2;2;2ma draft I typed myself\x1b[0m"
    )
    assert coloured != styled, "the styled draft fixture moved"

    assert classify(coloured, _descriptor("claude")) is PaneState.COMPOSING
    assert composer_draft(coloured, _descriptor("claude")) == "a draft I typed myself"


def test_a_hyperlink_at_the_head_of_a_dialog_line_does_not_hide_the_dialog() -> None:
    """Claude draws OSC 8 links; one opening a dialog's line must not unanchor its pattern."""
    dialog = (_PANES / "claude" / "dialog_approval.txt").read_text(encoding="utf-8")
    line = next(line for line in dialog.splitlines() if "Do you want to proceed?" in line)
    linked = dialog.replace(line, "\x1b]8;;https://example.invalid\x1b\\" + line, 1)
    assert linked != dialog

    assert classify(linked, _descriptor("claude")) is PaneState.DIALOG


@pytest.mark.parametrize("agent", ["claude", "codex", "cursor"])
def test_every_live_trust_dialog_is_a_declared_dialog(agent: str) -> None:
    """The check between a sequence's keys reads declared dialog patterns only, not the trust
    fallback; a live trust dialog must still be one it sees."""
    from remote_agents.adapters.tmux.composer import dialog_on_screen

    trust = (_PANES / agent / "dialog_trust.txt").read_text(encoding="utf-8")

    assert dialog_on_screen(trust, _descriptor(agent))


# --- Has the running turn ended? (BL-108) -------------------------------------------------------
#
# Captured 2026-09-24 from a real Claude 2.1.282 pane on the owner's own settings. Kept apart from
# the per-agent directories above, whose names say what the *screen alone* reads: a streaming
# answer reads IDLE there, which is the very gap the turn marker closes.
_TURN_STATES = _PANES / "turn_states"


def _turn_state(agent: str, name: str) -> str:
    return (_TURN_STATES / agent / f"{name}.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "ended"),
    [
        ("finished_footer", True),
        ("interrupted", True),
        ("streaming_answer", False),
        # The previous turn's footer is still on screen, above the new prompt and its answer:
        # only the last line above the input box counts.
        ("streaming_below_old_footer", False),
    ],
)
def test_claude_turn_ended_reads_the_last_line_above_the_box(name: str, ended: bool) -> None:
    assert turn_ended(_turn_state("claude", name), _descriptor("claude")) is ended


@pytest.mark.parametrize(
    ("name", "ended"),
    [
        ("idle_after_turn", True),
        ("idle_after_long_turn", True),
        ("idle_suggestion", True),
        ("composed_long", True),
        # A fresh session, and one whose last output is a slash command's: nothing says a turn
        # ended there, so a marker on such a screen (which no measured path leaves) holds.
        ("idle", False),
        ("idle_remote_control_disconnected", False),
        ("busy_plan_status", False),
        ("busy_starting", False),
    ],
)
def test_claude_turn_ended_on_the_existing_captures(name: str, ended: bool) -> None:
    screen = (_PANES / "claude" / f"{name}.txt").read_text(encoding="utf-8")

    assert turn_ended(screen, _descriptor("claude")) is ended


def test_a_right_aligned_hint_below_the_end_line_does_not_hide_it() -> None:
    """The interrupted capture has a right-aligned tmux tip between its end line and the box."""
    screen = _turn_state("claude", "interrupted")
    assert "tmux detected" in screen

    assert turn_ended(screen, _descriptor("claude")) is True


def test_an_end_line_that_is_not_last_does_not_count() -> None:
    finished = _turn_state("claude", "finished_footer")
    box = finished.index("\n────")
    streaming = f"{finished[:box]}\n● and one more line still streaming{finished[box:]}"

    assert turn_ended(streaming, _descriptor("claude")) is False


@pytest.mark.parametrize(("agent", "name"), [c for c in _captures() if c[0] == "codex"])
def test_codex_turn_ended_is_its_screen_and_title_reading_not_busy(agent: str, name: str) -> None:
    """Codex declares no end line: it has ended when nothing on screen or in the title says busy."""
    screen = (_PANES / agent / f"{name}.txt").read_text(encoding="utf-8")
    descriptor = _descriptor(agent)

    busy = classify(screen, descriptor) is PaneState.BUSY
    found = composer_draft(screen, descriptor) is not None
    assert turn_ended(screen, descriptor) is (found and not busy)
    assert turn_ended(screen, descriptor, title="⠋ codex") is False


def test_a_screen_with_no_composer_has_not_ended() -> None:
    assert turn_ended("", _descriptor("claude")) is False
    assert turn_ended("$ ls\n", _descriptor("codex")) is False
