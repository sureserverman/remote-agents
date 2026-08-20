"""Disposable live qualification of the fixed Claude Remote Control interaction."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import build_launch_profile
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.profiles import closed_profiles
from remote_agents.domain.remote_control import RemoteControlState


@pytest.mark.live_acceptance
async def test_claude_remote_control_toggle_on_an_exact_disposable_managed_pane(
    tmp_path: Path,
) -> None:
    if os.environ.get("REMOTE_AGENTS_LIVE_PROFILE") != "claude":
        pytest.skip("BLOCKED: claude_profile_not_selected")
    project_value = os.environ.get("REMOTE_AGENTS_LIVE_PROJECT")
    if project_value is None:
        pytest.skip("BLOCKED: trusted_project_not_configured")
    project_path = Path(project_value).resolve(strict=True)
    executable = shutil.which("claude")
    if executable is None:
        pytest.skip("BLOCKED: executable_missing")

    definition = next(
        profile for profile in closed_profiles() if profile.profile_id == ProfileId("claude")
    )
    session_id = SessionId.new()
    project_id = ProjectId("qualification")
    profile = build_launch_profile(
        definition,
        Path(executable),
        session_id,
        {
            key: os.environ[key]
            for key in ("HOME", "LANG", "LC_ALL", "PATH", "TERM")
            if key in os.environ
        },
    )
    socket = f"remote-agents-test-{session_id.value.hex}"
    gateway = TmuxGateway(socket, AsyncTmuxRunner(), intent_directory=tmp_path / "intents")
    terminal = TmuxTerminal(
        gateway,
        {project_id: project_path},
        {definition.profile_id: profile},
        startup_timeout=20,
    )
    try:
        launched = await terminal.launch(session_id, project_id, definition.profile_id)
        assert launched.live, launched.detail
        enabled_state = await terminal.remote_control(session_id, RemoteControlState.ACTIVE)
        assert enabled_state is RemoteControlState.ACTIVE

        disabled_state = await terminal.remote_control(session_id, RemoteControlState.INACTIVE)
        assert disabled_state is RemoteControlState.INACTIVE
    finally:
        try:
            await gateway.destroy(session_id)
        except RuntimeError:
            pass
