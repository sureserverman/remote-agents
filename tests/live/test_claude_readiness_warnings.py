"""A real Claude whose warnings hold its banner off the screen is still seen to have started.

The 2026-10-30 incident, reproduced through the real terminal and the real hook: thirty allow
rules Claude warns about keep `Claude Code` off an 80x24 pane. With the hook this is a clean
launch (DEC-135). Without it the launch answers `startup_timeout` after the whole budget, which
is what a host that has not re-run the installer still gets. No prompt is typed, so no quota.

`tmp_path` sits under `/tmp`, which this host trusts, so no dialog stands in the way. A launch
that does meet one fails `not awaiting_trust` below rather than passing for the wrong reason.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import install_agent_hooks
from remote_agents.adapters.agents.turn_markers import FileConversationMarks, FileStartedMarks
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import build_launch_profile
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.profiles import closed_profiles

_CLAUDE = ProfileId("claude")
_PROJECT = ProjectId("qualification")
_SCREEN = ("-x", "80", "-y", "24")


def _warning_rules(count: int = 30) -> list[str]:
    return [f"Bash(rm -rf /tmp/ra-probe-{i}*.deb /tmp/ra-probe-{i}-stock)" for i in range(count)]


@dataclass
class Rig:
    workspace: Path
    activity: Path
    session_id: SessionId
    gateway: TmuxGateway
    terminal: TmuxTerminal


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")
    executable = shutil.which("claude")
    if executable is None or shutil.which("tmux") is None:
        pytest.skip("BLOCKED: executable_missing")
    workspace = tmp_path / "workspace"
    (workspace / ".claude").mkdir(parents=True)
    (workspace / ".claude" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": _warning_rules()}})
    )
    activity = tmp_path / "activity"
    activity.mkdir(mode=0o700)
    session_id = SessionId.new()
    definition = next(p for p in closed_profiles() if p.profile_id == _CLAUDE)
    profile = build_launch_profile(
        definition,
        Path(executable),
        session_id,
        {k: os.environ[k] for k in ("HOME", "LANG", "LC_ALL", "PATH", "TERM") if k in os.environ},
    )
    gateway = TmuxGateway(
        f"remote-agents-test-{session_id.value.hex}",
        AsyncTmuxRunner(),
        intent_directory=tmp_path / "intents",
    )
    terminal = TmuxTerminal(
        gateway,
        {_PROJECT: workspace},
        {_CLAUDE: profile},
        startup_timeout=20,
        started_marks=FileStartedMarks(activity),
        conversation_marks=FileConversationMarks(activity),
    )
    yield Rig(workspace, activity, session_id, gateway, terminal)
    try:
        await gateway.destroy(session_id)
    except RuntimeError:
        pass


@pytest.mark.live_profile
async def test_the_hook_says_a_session_started_when_the_screen_cannot(rig: Rig) -> None:
    install_agent_hooks(
        rig.workspace / ".claude" / "settings.local.json",
        executable=Path(sys.executable),
        activity_directory=rig.activity,
    )

    launched = await rig.terminal.launch(rig.session_id, _PROJECT, _CLAUDE)

    screen = await rig.gateway.capture(rig.session_id)
    assert launched.live, f"{launched.detail}, on this screen:\n{screen}"
    assert not launched.awaiting_trust, screen
    assert FileStartedMarks(rig.activity).exists(str(rig.session_id)), screen
    assert "Claude Code" not in screen
    assert (await rig.terminal.confirm_ready(rig.session_id, _CLAUDE)).live


@pytest.mark.live_profile
async def test_with_no_hook_the_launch_answers_as_it_always_did(rig: Rig) -> None:
    launched = await rig.terminal.launch(rig.session_id, _PROJECT, _CLAUDE)

    screen = await rig.gateway.capture(rig.session_id)
    assert not launched.live, f"{launched.detail}, on this screen:\n{screen}"
    assert launched.detail == "startup_timeout"
    assert "Claude Code" not in screen
