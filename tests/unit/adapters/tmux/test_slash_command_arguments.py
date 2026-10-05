"""A `/` command with arguments is submitted only onto a screen positively showing no menu.

Claude hides its command menu once arguments follow the command (the 2026-10-05 live run,
`fixtures/panes/claude/composed_slash_arguments.txt`). The menu rule -- `Enter` only when the
menu's first entry is the command typed -- therefore refused every such command, the rollover's
own `/planning:executing-plans --adopt-handoff <id>` among them. With no menu open, `Enter`
submits the draft as typed, which is what the rule protects; so a command with arguments may go
in when no menu is drawn **and** the agent's `menu_absent` pattern positively says so. A bare
command, a drawn menu, or a screen that is not plainly menu-free is refused as before."""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import profile_composers
from remote_agents.adapters.tmux.composer import enter_refusal
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


@pytest.mark.parametrize("above", ["  ⎿  a hook said something", "  /status   a menu row"])
def test_anything_drawn_above_the_composer_fails_closed(above: str) -> None:
    """Not a menu this reads, and not the blank row that says there is none: refused."""
    screen = _with_menu_row(SCREEN, above)

    assert enter_refusal(screen, CLAUDE, TYPED) is PromptReason.MENU
