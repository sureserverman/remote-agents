"""Every Codex launch and resume is plain argv, whatever permission preset it carries.

Codex runs a TUI with any command-line config override on an embedded app server, off the
shared daemon (`codex-rs/tui/src/daemon_startup.rs` `exclusion()`, 0.167), so a preset passed as
a flag costs the session Codex Remote Control. This drives the production wiring --
`TmuxTerminal` with `composition.tui`'s factories -- and stops only at the step that would
start tmux.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.runtime import TmuxTerminal
from remote_agents.composition.tui import _profile_factory, _resume_profile_factory
from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.profiles import PermissionPreset, closed_profiles

CODEX = ProfileId("codex")
DEFINITION = next(p for p in closed_profiles() if p.profile_id == CODEX)
EXECUTABLE = Path("/usr/bin/codex")
PROJECT = ProjectId("fixture-project")


def _terminal(captured: list[tuple[str, ...]]) -> TmuxTerminal:
    terminal = TmuxTerminal(
        gateway=None,  # never reached: the launch stops before tmux
        project_paths={PROJECT: Path("/fixture")},
        profiles={},
        startup_timeout=1.0,
        profile_factories={CODEX: _profile_factory(DEFINITION, EXECUTABLE, {})},
        resume_profile_factories={CODEX: _resume_profile_factory(DEFINITION, EXECUTABLE, {})},
    )

    async def capture(session_id, project_id, profile_id, profile):
        captured.append(tuple(profile.argv))
        return None

    terminal._launch_profile = capture  # type: ignore[method-assign]
    return terminal


@pytest.mark.parametrize("preset", [None, *PermissionPreset])
def test_a_codex_launch_is_plain_for_every_preset(preset) -> None:
    captured: list[tuple[str, ...]] = []
    asyncio.run(_terminal(captured).launch(SessionId.new(), PROJECT, CODEX, permissions=preset))
    assert captured == [(str(EXECUTABLE),)]


@pytest.mark.parametrize("preset", [None, *PermissionPreset])
def test_a_codex_resume_is_plain_for_every_preset(preset) -> None:
    captured: list[tuple[str, ...]] = []
    source = ProviderConversationId("thread-123")
    asyncio.run(
        _terminal(captured).resume(SessionId.new(), PROJECT, CODEX, source, permissions=preset)
    )
    assert captured == [(str(EXECUTABLE), "resume", "thread-123")]
