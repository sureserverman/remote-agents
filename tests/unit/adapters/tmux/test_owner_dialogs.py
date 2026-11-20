"""A dialog only the owner can answer is named as one, and nothing is pressed on it (BL-146).

Codex 0.171.0 opens a fresh session on "Hooks need review" when its plugin hooks are new or
changed (`fixtures/panes/codex/dialog_hooks_review_0171.txt`, a real pane). Trusting hooks lets
them run outside the sandbox, so the answer is the owner's alone: the service reads the dialog,
names it on both surfaces, and never presses a key into it -- not a trust answer, not a decline.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from remote_agents.adapters.agents.registry import (
    profile_composers,
    profile_trust_dialogs,
    provider_descriptors,
)
from remote_agents.adapters.tmux.composer import PaneState, classify, owner_dialog
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import LaunchProfile, TerminalWaits, TmuxTerminal
from remote_agents.domain.models import ProfileId
from remote_agents.domain.trust import TrustState

from .test_send_prompt import PromptPane

_PANES = Path(__file__).resolve().parents[3] / "fixtures/panes"
HOOKS = (_PANES / "codex/dialog_hooks_review_0171.txt").read_text(encoding="utf-8")
FOLDER_TRUST = (_PANES.parent / "trust_dialogs/codex.txt").read_text(encoding="utf-8")
IDLE = (_PANES / "codex/idle_0158.txt").read_text(encoding="utf-8")
_CODEX = ProfileId("codex")


def _descriptor(profile: str):
    return next(d for d in provider_descriptors() if str(d.profile_id) == profile)


def _terminal(pane: PromptPane) -> TmuxTerminal:
    return TmuxTerminal(
        TmuxGateway("remote-agents-test-owner-dialog", pane),
        {},
        {
            _CODEX: LaunchProfile(
                "/usr/bin/codex", ("/usr/bin/codex",), {}, None, graceful_keys=("/exit", "Enter")
            )
        },
        startup_timeout=0.05,
        composers=profile_composers(),
        trust_dialogs=profile_trust_dialogs(),
        waits=TerminalWaits(trust_answer=0.0, trust_cursor=0.05, trust_hold=0.0, decline=0.05),
    )


def test_the_captured_hook_review_is_codex_s_owner_dialog() -> None:
    found = owner_dialog(HOOKS, _descriptor("codex"))

    assert found is not None
    assert found.names == "a hook review"


def test_the_folder_trust_dialog_and_an_idle_composer_are_not_owner_dialogs() -> None:
    codex = _descriptor("codex")

    assert owner_dialog(FOLDER_TRUST, codex) is None
    assert owner_dialog(IDLE, codex) is None


def test_an_owner_dialog_is_still_a_dialog_to_the_prompt_relay() -> None:
    """Named apart, never typed into: the relay refuses it as it refuses every dialog."""
    assert classify(HOOKS, _descriptor("codex")) is PaneState.DIALOG


def test_only_codex_declares_an_owner_dialog() -> None:
    """Measured on Codex alone; another agent's screen is never read with Codex's patterns."""
    declaring = {
        str(d.profile_id)
        for d in provider_descriptors()
        if d.composer is not None and d.composer.owner_dialogs
    }

    assert declaring == {"codex"}
    assert owner_dialog(HOOKS, _descriptor("claude")) is None


def test_the_terminal_names_the_owner_dialog_on_a_codex_pane() -> None:
    pane = PromptPane([HOOKS], profile="codex")

    assert asyncio.run(_terminal(pane).owner_dialog(pane.session_id)) == "a hook review"
    assert pane.typed == []


def test_the_terminal_names_nothing_on_a_folder_trust_dialog() -> None:
    pane = PromptPane([FOLDER_TRUST], profile="codex")

    assert asyncio.run(_terminal(pane).owner_dialog(pane.session_id)) is None


def test_the_terminal_names_nothing_for_a_pane_that_is_gone() -> None:
    pane = PromptPane([HOOKS], profile="codex", listed=False)

    assert asyncio.run(_terminal(pane).owner_dialog(pane.session_id)) is None


def test_the_trust_answer_presses_no_key_on_an_owner_dialog() -> None:
    pane = PromptPane([HOOKS], profile="codex")

    answer = asyncio.run(_terminal(pane).answer_trust(pane.session_id))

    assert answer.pressed is False
    assert pane.typed == []


def test_the_trust_state_of_an_owner_dialog_is_not_awaiting() -> None:
    """So no surface offers the folder-trust row for it (`trust_available`)."""
    pane = PromptPane([HOOKS], profile="codex")

    assert asyncio.run(_terminal(pane).trust_state(pane.session_id)) is not TrustState.AWAITING


def test_a_pane_that_only_shows_the_words_is_no_owner_dialog() -> None:
    """Stage 3 gate finding: a diff, a transcript or this repo's own fixture on screen."""
    codex = _descriptor("codex")
    quoted = (
        'GOT: grep says r"Hooks need review" in codex/__init__.py\n'
        "  Hooks need review\n"
        "› Ask Codex to do anything\n"
        "  fake-model default · opaque-editor\n"
    )

    assert owner_dialog(quoted, codex) is None
    pane = PromptPane([quoted], profile="codex")
    assert asyncio.run(_terminal(pane).owner_dialog(pane.session_id)) is None


def test_the_prompt_relay_types_nothing_onto_the_hook_review() -> None:
    """The property every fail-open look leans on (Stage 3 gate): a send refuses the screen."""
    pane = PromptPane([HOOKS], profile="codex")

    delivery = asyncio.run(_terminal(pane).send_prompt(pane.session_id, "hello"))

    assert delivery.outcome.value == "refused"
    assert pane.typed == []
