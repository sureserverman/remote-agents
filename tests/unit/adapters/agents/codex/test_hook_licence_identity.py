"""Codex's hook runs in a shared daemon, so its licence says the payload names the session."""

from __future__ import annotations

import pytest

from remote_agents.adapters.agents.hook_licences import hook_licence
from remote_agents.agent_event import HOOK_PROVIDERS
from remote_agents.ports.provider_descriptor import HookIdentity


def test_codex_names_its_session_from_the_payload_thread() -> None:
    """Measured 2026-10-13: Codex 0.162.1 spawns hooks from its app-server daemon (DEC-126)."""
    licence = hook_licence("codex")

    assert licence is not None
    assert licence.identity is HookIdentity.PAYLOAD_THREAD


@pytest.mark.parametrize("provider", [name for name in HOOK_PROVIDERS if name != "codex"])
def test_every_other_hook_keeps_the_panes_environment(provider: str) -> None:
    """Their hooks run as children of the agent in the managed pane, which carries the id."""
    licence = hook_licence(provider)

    assert licence is not None
    assert licence.identity is HookIdentity.ENVIRONMENT
