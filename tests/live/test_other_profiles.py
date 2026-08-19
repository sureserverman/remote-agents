"""Opt-in qualification of the remaining interactive profiles without a task prompt."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import build_launch_profile
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.domain.models import ProjectId, SessionId
from remote_agents.domain.profiles import closed_profiles


@pytest.mark.live_profile
@pytest.mark.parametrize("profile_id", ("codex", "opencode", "cursor-agent"))
async def test_other_profile_live_lifecycle(
    tmp_path: Path, request: pytest.FixtureRequest, profile_id: str
) -> None:
    selected = request.config.getoption("profile")
    if profile_id not in selected:
        pytest.skip("profile was not selected")
    project_value = os.environ.get("REMOTE_AGENTS_LIVE_PROJECT")
    if project_value is None:
        pytest.skip("BLOCKED: trusted_project_not_configured")
    project_path = Path(project_value).resolve(strict=True)
    if not project_path.is_dir():
        pytest.skip("BLOCKED: trusted_project_missing")
    definition = next(
        profile for profile in closed_profiles() if str(profile.profile_id) == profile_id
    )
    executable = shutil.which(definition.executable)
    if executable is None:
        pytest.skip("BLOCKED: executable_missing")
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
        startup_timeout=15,
    )
    try:
        launched = await terminal.launch(session_id, project_id, definition.profile_id)
        assert launched.live, launched.detail
        assert (await terminal.inspect(session_id)).live

        stopped = await terminal.graceful_stop(session_id, definition.profile_id)
        assert stopped.preserved, stopped.detail
        await terminal.cleanup(session_id)
        assert await terminal.inspect(session_id) is None
    finally:
        try:
            await gateway.destroy(session_id)
        except RuntimeError:
            pass
