"""Disposable live qualification of the one-button Claude Remote Control toggle.

Three readings and two presses against one real `claude` pane, on a socket this test
creates and destroys. The two presses are the *same* button: what decides the direction is
`TerminalPort.remote_control_state`, read between the press and the question, resolved by
`remote_control_target`. That pairing is the surface's whole behaviour with the surface
taken out, which is what makes it drillable here at all.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import build_launch_profile
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.application.session_actions import remote_control_target
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

        # --- Press one: a pane nobody has toggled ------------------------------------------
        #
        # This is the UNKNOWN case, taken from a real pane rather than contrived: a freshly
        # launched Claude shows none of the three markers, so the read that a confirmation
        # takes answers UNKNOWN, and the policy offers *on* only. The plan suggested reaching
        # UNKNOWN by scrolling the marker out of the capture; a pane that has never been
        # toggled is the same reading, is what production actually meets first, and is
        # deterministic here instead of depending on how tall the pane happens to be.
        assert await terminal.remote_control_state(session_id) is RemoteControlState.UNKNOWN
        first = remote_control_target(RemoteControlState.UNKNOWN)
        assert first is RemoteControlState.ACTIVE, (
            "an unreadable pane must be offered on -- disabling opens Claude's status menu "
            "and arrowing through a menu we cannot see is what DEC-003 refuses"
        )
        enabled_state = await terminal.remote_control(session_id, first)
        assert enabled_state is RemoteControlState.ACTIVE

        # --- Press two: the same button, the other way -------------------------------------
        #
        # Nothing about the button changed between the two presses. What changed is what the
        # pane says, which is the whole claim the single toggle rests on.
        observed = await terminal.remote_control_state(session_id)
        assert observed is RemoteControlState.ACTIVE, "the read must see the press that landed"
        disabled_state = await terminal.remote_control(session_id, remote_control_target(observed))
        assert disabled_state is RemoteControlState.INACTIVE

        # --- And the reading follows it back -----------------------------------------------
        assert await terminal.remote_control_state(session_id) is RemoteControlState.INACTIVE
    finally:
        try:
            await gateway.destroy(session_id)
        except RuntimeError:
            pass
