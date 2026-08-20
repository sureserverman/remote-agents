"""Opt-in proof that a real agent's idle pane stops changing, so quiet can ever be detected.

Pane quiet is decided by digesting a capture and comparing it with the previous one, and every
other test of that path drives a plain-stdout script. The three profiles it actually serves are
full-screen TUIs. If any of them keeps something moving in its idle frame -- an elapsed timer, a
token counter, a spinner that persists at the prompt -- the digest changes on every poll, the
signal never fires, and nothing anywhere reports a problem. A silent false negative in the one
direction no unit test can see, because the fixture is the thing being assumed.

So this measures the assumption against the real binaries: launch, let the agent settle, then
capture repeatedly and require the captures to be identical. It is the same shape as every other
file here -- `REMOTE_AGENTS_LIVE_ACCEPTANCE=1`, `BLOCKED:` skips, its own tmux socket -- and it
needs no task prompt, because an idle agent is exactly the state under test.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import build_launch_profile
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.application.activity import observe_quiet
from remote_agents.domain.models import ProjectId, SessionId
from remote_agents.domain.profiles import closed_profiles
from remote_agents.ports.agent_activity import HOOK_SOURCED_PROFILES

_SETTLE_SECONDS = 8.0
_POLL_SECONDS = 2.0


@pytest.mark.live_profile
@pytest.mark.parametrize("profile_id", sorted({"codex", "opencode", "cursor-agent"}))
async def test_an_idle_agent_pane_settles_enough_for_quiet_to_be_detected(
    tmp_path: Path, profile_id: str
) -> None:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")
    assert profile_id not in HOOK_SOURCED_PROFILES, "this is only meaningful for the pane path"
    project_value = os.environ.get("REMOTE_AGENTS_LIVE_PROJECT")
    if project_value is None:
        pytest.skip("BLOCKED: trusted_project_not_configured")
    project_path = Path(project_value).resolve(strict=True)
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
        startup_timeout=45,
    )
    try:
        launched = await terminal.launch(session_id, project_id, definition.profile_id)
        if not launched.live:
            pytest.skip(f"BLOCKED: {launched.detail}")

        # Let the start-up banner, any first-run notice and the initial redraw finish. What is
        # being measured is the *settled* idle frame, not the moments after launch.
        await asyncio.sleep(_SETTLE_SECONDS)

        watch = None
        reports = []
        captures = []
        for _ in range(4):
            capture = await terminal.capture(session_id)
            captures.append(capture)
            watch, activity = observe_quiet(
                watch,
                session_id=str(session_id),
                capture=capture,
                now=__import__("datetime").datetime.now(__import__("datetime").UTC),
                quiet_polls=2,
            )
            if activity is not None:
                reports.append(activity)
            await asyncio.sleep(_POLL_SECONDS)

        distinct = {capture for capture in captures}
        assert len(distinct) == 1, (
            f"{profile_id}'s idle pane never settles: {len(distinct)} distinct captures across "
            f"{len(captures)} polls. Pane quiet cannot be detected for this profile, and the "
            "failure would otherwise be silent."
        )
    finally:
        try:
            await gateway.destroy(session_id)
        except RuntimeError:
            pass
