"""Codex's composer holding the rollover's `$` adopt message, read and submitted for real.

The rollover pass types a Codex successor `$planning:executing-plans --adopt-handoff <id>`
(the Codex vertical's `adopt_template`). Codex raises a skill-mention popup while a `$` mention
is being typed, and `Enter` there inserts the highlighted entry rather than submitting. So the
capture `codex/composed_skill_mention.txt` -- a real 0.162.1 pane with the whole message pasted
as the guarded send pastes it -- must read as COMPOSING with the draft intact, and the send must
press its one `Enter` on it. What the capture showed, and why no extra key is needed, is in its
sidecar `composed_skill_mention.md`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from remote_agents.adapters.agents.codex import descriptor
from remote_agents.adapters.tmux.composer import PaneState, classify, composer_draft
from remote_agents.ports.terminal import PromptOutcome

from .test_send_prompt import PromptPane, _screen, _terminal

_PANES = Path(__file__).resolve().parents[3] / "fixtures" / "panes"
_HANDOFF = "h-0123456789abcdef0123"


def _adopt_message() -> str:
    template = descriptor().adopt_template
    assert template is not None
    return template.format(handoff_id=_HANDOFF)


def test_skill_mention_capture_reads_as_composing_with_the_adopt_message_intact() -> None:
    screen = (_PANES / "codex" / "composed_skill_mention.txt").read_text(encoding="utf-8")

    assert classify(screen, descriptor()) is PaneState.COMPOSING
    assert composer_draft(screen, descriptor()) == _adopt_message()


def test_skill_mention_draft_is_submitted_by_the_guarded_send_with_one_enter() -> None:
    pane = PromptPane(
        [
            _screen("codex", "idle"),
            _screen("codex", "composed_skill_mention"),
            _screen("codex", "busy"),
        ],
        profile="codex",
    )

    delivery = asyncio.run(_terminal(pane).send_prompt(pane.session_id, _adopt_message()))

    assert delivery.outcome is PromptOutcome.SENT
    assert pane.stdin == [_adopt_message()]
    assert pane.keys == ["Enter"], "one Enter, and no key to close a popup: none is drawn"


def test_skill_mention_popup_on_a_bare_prefix_still_reads_its_draft() -> None:
    """The one screen that draws the popup: a bare `$plan`, captured on the same 0.162.1 pane.
    The popup sits above the composer and leaves the model line, so the composer and its draft
    still read. `Enter` here would insert the highlighted skill, which only a relayed bare `$`
    prefix could meet -- the adopt template always carries text after its mention."""
    screen = (_PANES / "codex" / "composed_skill_popup.txt").read_text(encoding="utf-8")

    assert "enter insert · esc close" in screen
    assert classify(screen, descriptor()) is PaneState.COMPOSING
    assert composer_draft(screen, descriptor()) == "$plan"
