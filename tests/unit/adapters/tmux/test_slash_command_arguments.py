"""A `/` command with arguments is submitted only onto a screen positively showing no menu.

Claude hides its command menu once arguments follow the command (the 2026-10-05 live run,
`fixtures/panes/claude/composed_slash_arguments.txt`). The menu rule -- `Enter` only when the
menu's first entry is the command typed -- therefore refused every such command, the rollover's
own `/planning:executing-plans --adopt-handoff <id>` among them. With no menu open, `Enter`
submits the draft as typed, which is what the rule protects; so a command with arguments may go
in when no menu is drawn **and** the agent's `menu_absent` pattern positively says so. A bare
command, a drawn menu, or a screen that is not plainly menu-free is refused as before."""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import profile_composers
from remote_agents.adapters.tmux.composer import enter_refusal, unstyled
from remote_agents.ports.terminal import PromptReason

_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures/panes/claude"
SCREEN = (_FIXTURES / "composed_slash_arguments.txt").read_text(encoding="utf-8")
TYPED = (
    "/planning:executing-plans /tmp/pytest-of-user/pytest-662/test_rollover_now_hands_a_real0/"
    "ra-rollover-5sd0jh8k/docs/rollover-fixture-plan.md"
)
_DRAFT = (
    "❯\xa0/planning:executing-plans /tmp/pytest-of-user/pytest-662/test_rollover_now_h\n"
    "  ands_a_real0/ra-rollover-5sd0jh8k/docs/rollover-fixture-plan.md"
)
CLAUDE = profile_composers()["claude"]


def _with_draft(draft_line: str) -> str:
    assert _DRAFT in SCREEN
    return SCREEN.replace(_DRAFT, draft_line)


def test_the_fixture_holds_the_draft_it_names() -> None:
    assert _DRAFT in SCREEN


def test_a_command_with_arguments_goes_in_onto_a_screen_with_no_menu() -> None:
    assert enter_refusal(SCREEN, CLAUDE, TYPED) is None


def test_the_rollover_template_goes_in_the_same_way() -> None:
    template = "/planning:executing-plans --adopt-handoff h-0123456789abcdef0123"
    screen = _with_draft(f"❯\xa0{template}")

    assert enter_refusal(screen, CLAUDE, template) is None


def test_a_bare_command_still_needs_its_menu() -> None:
    """No arguments: Claude draws a menu for it, so one that is not read is not one that agreed."""
    screen = _with_draft("❯\xa0/status")

    assert enter_refusal(screen, CLAUDE, "/status") is PromptReason.MENU


def _with_menu_row(screen: str, row: str) -> str:
    """`screen` with `row` drawn on the line directly above the composer's top rule."""
    lines = screen.splitlines()
    rule = max(index for index, line in enumerate(lines) if line.startswith("❯")) - 1
    lines[rule - 1] = row
    return "\n".join(lines) + "\n"


_STATUS_ROW = "  /status                         Show Claude Code status"


def test_a_drawn_menu_still_decides_a_command_with_arguments() -> None:
    """A menu is drawn, so it is read: its first entry must be the command, arguments or not."""
    agreeing = _with_menu_row(_with_draft("❯\xa0/status now"), _STATUS_ROW)
    disagreeing = _with_menu_row(_with_draft("❯\xa0/statu now"), _STATUS_ROW)

    assert enter_refusal(agreeing, CLAUDE, "/status now") is None
    assert enter_refusal(disagreeing, CLAUDE, "/statu now") is PromptReason.MENU


def test_a_menu_row_above_the_composer_refuses() -> None:
    screen = _with_menu_row(SCREEN, "  /status   a menu row")

    assert enter_refusal(screen, CLAUDE, TYPED) is PromptReason.MENU


def test_a_menu_the_menu_pattern_cannot_read_still_refuses() -> None:
    """A `  /` row the menu pattern does not read -- here, a row it does not recognise sits
    between it and the composer -- is not proof there is no menu: refused, never guessed at."""
    lines = SCREEN.splitlines()
    rule = max(index for index, line in enumerate(lines) if line.startswith("❯")) - 1
    lines[rule - 2 : rule] = ["  /status   a menu row", "a row the menu pattern does not read"]
    screen = "\n".join(lines) + "\n"

    assert enter_refusal(screen, CLAUDE, TYPED) is PromptReason.MENU


@pytest.mark.parametrize(
    "above",
    [
        "  ⎿  a hook said something",
        "rollover-fixture ▐█████░░░░░▌ 1/2 · S1/2 ⏸ HANDOFF rule reques… [ Plan ]    [-]",
        "                                                              ● high · /effort",
    ],
    ids=["hook-line", "planning-band", "effort-hint"],
)
def test_rows_that_are_not_a_menu_do_not_block_it(above: str) -> None:
    """The property is "no menu row", not "a blank row": the planning band and the effort hint
    sat there on the 2026-10-05 run that a blank-row rule refused."""
    screen = _with_menu_row(SCREEN, above)

    assert enter_refusal(screen, CLAUDE, TYPED) is None


_TEMPLATE = "/planning:executing-plans --adopt-handoff h-0123456789abcdef0123"

#: Every real Claude capture, with the rollover template pasted into its composer: what the
#: guard must say. Allowed exactly where no menu is drawn and the composer holds the draft; a
#: drawn menu refuses, and a busy, shell-mode or dialog screen refuses for its own reason.
_SWEEP = {
    "busy.txt": PromptReason.DRAFT_NOT_SEEN,
    "busy_plan_status.txt": PromptReason.DRAFT_NOT_SEEN,
    "busy_starting.txt": PromptReason.DRAFT_NOT_SEEN,
    "busy_tool.txt": PromptReason.DRAFT_NOT_SEEN,
    "composed.txt": None,
    "composed_long.txt": None,
    "composed_shell_mode.txt": PromptReason.DRAFT_NOT_SEEN,
    "composed_slash.txt": PromptReason.MENU,
    "composed_slash_arguments.txt": None,
    "composed_slash_arguments_banded.txt": None,
    "composed_styled.txt": None,
    "dialog_after_pasted_y.txt": PromptReason.DIALOG,
    "dialog_approval.txt": PromptReason.DIALOG,
    "idle.txt": None,
    "idle_after_long_turn.txt": None,
    "idle_after_turn.txt": None,
    "idle_manual.txt": None,
    "idle_multiline_status.txt": None,
    "idle_narrow.txt": None,
    "idle_remote_control_disconnected.txt": None,
    "idle_remote_control_enabled.txt": PromptReason.MENU,
    "idle_suggestion.txt": None,
}


def _pasted(name: str) -> str:
    lines = unstyled((_FIXTURES / name).read_text(encoding="utf-8")).splitlines()
    composer = max(i for i, line in enumerate(lines) if line.startswith("❯"))
    end = next(j for j in range(composer + 1, len(lines)) if re.match(r"^─{10,}", lines[j]))
    return "\n".join([*lines[:composer], f"❯\xa0{_TEMPLATE}", *lines[end:]]) + "\n"


def test_the_sweep_covers_every_claude_capture_with_a_composer() -> None:
    with_composer = {
        path.name
        for path in _FIXTURES.glob("*.txt")
        if any(
            line.startswith("❯") for line in unstyled(path.read_text(encoding="utf-8")).splitlines()
        )
    }
    assert with_composer == set(_SWEEP)


@pytest.mark.parametrize("name", sorted(_SWEEP))
def test_every_real_capture_is_judged_as_its_screen_says(name: str) -> None:
    assert enter_refusal(_pasted(name), CLAUDE, _TEMPLATE) is _SWEEP[name]


def test_the_menu_free_check_is_linear_in_the_screen() -> None:
    """A long line above the composer must not make the pattern backtrack: one first draft did,
    and hung the guarded send."""
    screen = _with_menu_row(SCREEN, "x " * 4000 + "y")
    started = time.monotonic()

    enter_refusal(screen, CLAUDE, TYPED)

    assert time.monotonic() - started < 0.5
