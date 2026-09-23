"""Fixed key sequences never land on a composer that holds text, and a held lock is not a crash.

A graceful stop types `/exit` + Enter (Claude), and the Remote Control toggle `/remote-control` +
Enter. Into a composer already holding text -- a relayed message that was pasted but not submitted,
or the owner's own half-typed draft -- the Enter would submit `<draft>/exit` as a prompt: the stop
does not happen and the draft starts a turn. So both refuse on a composer holding text.

And both take the per-session key lock (BL-056), whose bounded wait raises `KeysBusy`. That used to
escape the four callers as an exception, leaving a stop record at STOP_REQUESTED.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from remote_agents.adapters.agents.registry import provider_descriptors
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.key_lock import KeysBusy
from remote_agents.adapters.tmux.runtime import LaunchProfile, TerminalWaits, TmuxTerminal
from remote_agents.domain.models import ProfileId
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.ports.terminal import COMPOSER_HOLDS_TEXT, KEYS_BUSY

from .test_send_prompt import PromptPane

_PANES = Path(__file__).resolve().parents[3] / "fixtures" / "panes"


def _screen(name: str) -> str:
    return (_PANES / "claude" / f"{name}.txt").read_text(encoding="utf-8")


def _terminal(pane: PromptPane) -> TmuxTerminal:
    composers = {
        str(descriptor.profile_id): descriptor
        for descriptor in provider_descriptors()
        if descriptor.composer is not None
    }
    profile = LaunchProfile(
        "/usr/bin/claude", ("/usr/bin/claude",), {}, None, graceful_keys=("/exit", "Enter")
    )
    return TmuxTerminal(
        TmuxGateway("remote-agents-test-draft", pane),
        {},
        {ProfileId("claude"): profile},
        startup_timeout=0.05,
        composers=composers,
        waits=TerminalWaits(prompt_settle=0.0, prompt_bound=2.0),
    )


def test_a_graceful_stop_is_not_sent_into_a_composer_holding_text() -> None:
    pane = PromptPane([_screen("composed")])

    observation = asyncio.run(_terminal(pane).graceful_stop(pane.session_id, ProfileId("claude")))

    assert observation.detail == COMPOSER_HOLDS_TEXT
    assert observation.live and not observation.preserved
    assert pane.keys == [], "the Enter would have submitted the draft as a prompt"


def test_a_graceful_stop_still_goes_to_an_idle_or_busy_pane() -> None:
    for screen in ("idle", "busy"):
        pane = PromptPane([_screen(screen)])
        asyncio.run(_terminal(pane).graceful_stop(pane.session_id, ProfileId("claude")))
        assert pane.keys == ["/exit", "Enter"], screen


def test_remote_control_does_not_type_into_a_composer_holding_text() -> None:
    pane = PromptPane([_screen("composed")])

    state = asyncio.run(_terminal(pane).remote_control(pane.session_id, RemoteControlState.ACTIVE))

    assert state is RemoteControlState.UNKNOWN
    assert pane.keys == []


def _held(monkeypatch) -> None:
    from remote_agents.adapters.tmux import gateway as gateway_module

    class Held:
        async def __aenter__(self):
            raise KeysBusy("another process is still typing into this pane")

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(gateway_module.TmuxGateway, "_keys_for", lambda self, session: Held())


def test_a_held_lock_makes_a_stop_never_sent_not_a_crash(monkeypatch) -> None:
    _held(monkeypatch)
    pane = PromptPane([_screen("idle")])

    observation = asyncio.run(_terminal(pane).graceful_stop(pane.session_id, ProfileId("claude")))

    assert observation.detail == KEYS_BUSY and observation.live


def test_a_held_lock_makes_remote_control_unknown_not_a_crash(monkeypatch) -> None:
    _held(monkeypatch)
    pane = PromptPane([_screen("idle")])

    state = asyncio.run(_terminal(pane).remote_control(pane.session_id, RemoteControlState.ACTIVE))

    assert state is RemoteControlState.UNKNOWN


def test_both_new_stop_details_record_a_stop_that_was_never_sent() -> None:
    from remote_agents.application.services import _STOP_EVENTS
    from remote_agents.domain.state_machine import LifecycleEvent

    assert _STOP_EVENTS[COMPOSER_HOLDS_TEXT] is LifecycleEvent.GRACEFUL_STOP_NEVER_SENT
    assert _STOP_EVENTS[KEYS_BUSY] is LifecycleEvent.GRACEFUL_STOP_NEVER_SENT
