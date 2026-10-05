"""The rollover's stop (DEC-115): sent only onto an idle composer, judged under the key lock the
keys are sent under, and never by interrupting a turn. Anything else is refused as `not_idle`
with nothing typed, so a turn that started after the rollover looked is left running."""

from __future__ import annotations

import asyncio

import pytest

from remote_agents.ports.terminal import NOT_IDLE

from .test_graceful_stop_never_sent import _PROFILE, _claude_pane, _composed_terminal


@pytest.mark.parametrize(
    "screen",
    ["busy", "busy_tool", "busy_starting", "composed", "composed_shell_mode", "dialog_approval"],
)
def test_a_pane_that_is_not_idle_gets_no_key_at_all(screen: str) -> None:
    pane = _claude_pane(screen)

    observation = asyncio.run(
        _composed_terminal(pane).graceful_stop(pane.session_id, _PROFILE, only_if_idle=True)
    )

    assert pane.keys == [], "not even the Esc that interrupts a running turn"
    assert observation.detail == NOT_IDLE
    assert observation.live and not observation.preserved


@pytest.mark.parametrize("screen", ["idle", "idle_after_turn", "idle_narrow"])
def test_an_idle_pane_gets_the_exit_keys(screen: str) -> None:
    pane = _claude_pane(screen)

    asyncio.run(
        _composed_terminal(pane).graceful_stop(pane.session_id, _PROFILE, only_if_idle=True)
    )

    assert pane.keys == ["/exit", "Enter"]


def test_without_the_guard_a_busy_pane_is_still_interrupted_and_stopped() -> None:
    """The owner's stop is unchanged: it has to reach an agent mid-turn."""
    pane = _claude_pane("busy")

    asyncio.run(_composed_terminal(pane).graceful_stop(pane.session_id, _PROFILE))

    assert pane.keys, "the owner's stop sends its keys onto a running turn"


@pytest.mark.parametrize(
    ("screen", "idle"),
    [
        ("idle", True),
        ("idle_after_turn", True),
        ("busy", False),
        ("composed", False),
        ("dialog_approval", False),
    ],
)
def test_the_idle_look_reads_the_composer(screen: str, idle: bool) -> None:
    pane = _claude_pane(screen)

    assert asyncio.run(_composed_terminal(pane).pane_idle(pane.session_id)) is idle
    assert pane.keys == [], "a look types nothing"


def test_a_look_at_a_pane_that_went_away_reads_not_idle() -> None:
    from pathlib import Path

    from .test_send_prompt import PromptPane

    fixtures = Path(__file__).resolve().parents[3] / "fixtures/panes/claude"
    pane = PromptPane([(fixtures / "idle.txt").read_text()], fail_on="capture-pane")

    assert asyncio.run(_composed_terminal(pane).pane_idle(pane.session_id)) is False


def test_an_agent_that_declares_an_interrupt_is_never_interrupted_by_this_stop() -> None:
    """Codex declares `Escape` to end a running turn, and the owner's stop presses it first.
    The rollover's stop never does: a busy Codex pane gets no key at all."""
    from pathlib import Path

    from remote_agents.adapters.agents.registry import profile_composers
    from remote_agents.adapters.tmux.gateway import TmuxGateway
    from remote_agents.adapters.tmux.runtime import LaunchProfile, TmuxTerminal
    from remote_agents.domain.models import ProfileId

    from .test_send_prompt import PromptPane

    busy = Path(__file__).resolve().parents[3] / "fixtures/panes/codex/busy.txt"
    pane = PromptPane([busy.read_text(encoding="utf-8")], profile="codex", title="⠏ ⠏ | w")
    codex = ProfileId("codex")
    terminal = TmuxTerminal(
        TmuxGateway("remote-agents-test-graceful", pane),
        {},
        {
            codex: LaunchProfile(
                "/usr/bin/codex",
                ("/usr/bin/codex",),
                {},
                None,
                graceful_keys=("/exit", "Enter", "Enter"),
            )
        },
        startup_timeout=0.05,
        composers=profile_composers(),
    )

    asyncio.run(terminal.graceful_stop(pane.session_id, codex))
    assert "Escape" in pane.keys, "the owner's stop does interrupt: the test can see it"
    pane.calls.clear()

    observation = asyncio.run(terminal.graceful_stop(pane.session_id, codex, only_if_idle=True))

    assert pane.keys == []
    assert observation.detail == NOT_IDLE


def test_a_pane_that_goes_before_its_title_is_read_is_a_stop_never_sent() -> None:
    from pathlib import Path

    from remote_agents.ports.terminal import UNKNOWN_SESSION

    from .test_send_prompt import PromptPane

    fixtures = Path(__file__).resolve().parents[3] / "fixtures/panes/claude"
    pane = PromptPane([(fixtures / "idle.txt").read_text()], fail_on="#{pane_title}")

    observation = asyncio.run(
        _composed_terminal(pane).graceful_stop(pane.session_id, _PROFILE, only_if_idle=True)
    )

    assert observation.detail == UNKNOWN_SESSION and not observation.live
    assert pane.keys == []
