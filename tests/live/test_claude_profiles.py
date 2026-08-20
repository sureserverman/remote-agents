"""Opt-in qualification of Claude's two interactive modes without sending a prompt."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import build_launch_profile
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.domain.models import ProjectId, SessionId
from remote_agents.domain.profiles import closed_profiles


@pytest.mark.live_profile
@pytest.mark.parametrize("profile_id", ("claude", "claude-remote"))
async def test_claude_profile_live_lifecycle(
    tmp_path: Path, request: pytest.FixtureRequest, profile_id: str
) -> None:
    selected = request.config.getoption("profile")
    if profile_id not in selected:
        pytest.skip("profile was not selected")
    trusted_root_value = os.environ.get("REMOTE_AGENTS_LIVE_PROJECT_ROOT")
    if trusted_root_value is None:
        pytest.skip("BLOCKED: trusted_project_root_not_configured")
    trusted_root = Path(trusted_root_value).resolve(strict=True)
    if not trusted_root.is_dir():
        pytest.skip("BLOCKED: trusted_project_root_missing")
    executable = shutil.which("claude")
    if executable is None:
        pytest.skip("BLOCKED: executable_missing")
    definition = next(
        profile for profile in closed_profiles() if str(profile.profile_id) == profile_id
    )
    session_id = SessionId.new()
    project_id = ProjectId("qualification")
    project_path = Path(tempfile.mkdtemp(prefix="remote-agents-qualification-", dir=trusted_root))
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
        shutil.rmtree(project_path)
