"""Live drill: a real approval dialog refuses Remote Control and a stop, and an idle pane stops.

Both key sequences end in `Enter`, and a real Claude approval dialog opens on its yes option, so
either one sent into it approves a command nobody approved (BL-055; DEC-063's never-approve
clause). The unit tests pin the guard against captured screens; this drives the real
`TmuxTerminal` against a real Claude pane that is showing a real dialog, and checks that nothing
reached it -- the dialog is still up and the command it asked about never ran. Then the drill
declines the dialog itself, and at the idle composer the same `graceful_stop` exits the agent.

**Not opt-in**, like `test_prompt_relay.py` and for its reason: it is a stage gate's command. It
skips only for what the host lacks -- `claude`, `tmux`, or Claude's credentials. It spends one
short Sonnet turn. Claude runs in **manual** permission mode here (the owner's panes run auto,
which never asks), with the owner's HOME, because a fresh config directory demands a login.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from agent_panes import CLAUDE_OPENING, CLAUDE_READY, open_to_composer

from remote_agents.adapters.agents.registry import profile_composers
from remote_agents.adapters.tmux.composer import PaneState, classify
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import (
    AsyncTmuxRunner,
    LaunchProfile,
    TerminalWaits,
    TmuxTerminal,
)
from remote_agents.domain.models import ProfileId, SessionId
from remote_agents.domain.profiles import closed_profiles
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.ports.terminal import AGENT_ASKING

_MARKER = "keyed-drill-approved.txt"
_ASK = f"Run the shell command `touch {_MARKER}` in this directory, then reply: done."
_CLAUDE = ProfileId("claude")


def _tmux(socket: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", socket, *arguments], capture_output=True, text=True, timeout=60, check=False
    )


def _requirements() -> None:
    for needed in ("claude", "tmux"):
        if shutil.which(needed) is None:
            pytest.skip(f"BLOCKED: executable_missing: {needed}")
    if not (Path.home() / ".claude" / ".credentials.json").is_file():
        pytest.skip("BLOCKED: claude is not logged in")


def _screen(socket: str, pane: str) -> str:
    return _tmux(socket, "capture-pane", "-p", "-e", "-t", pane).stdout


def _wait_for_state(socket: str, pane: str, wanted: PaneState, seconds: float) -> str:
    descriptor = profile_composers()["claude"]
    deadline = time.monotonic() + seconds
    screen = ""
    while time.monotonic() < deadline:
        screen = _screen(socket, pane)
        if classify(screen, descriptor) is wanted:
            return screen
        time.sleep(0.5)
    pytest.fail(f"the pane never read {wanted.name}:\n{screen}")


def test_a_real_dialog_refuses_both_and_an_idle_pane_takes_the_stop(tmp_path: Path) -> None:
    _requirements()
    session_id = SessionId.new()
    socket = f"remote-agents-test-keyed-{session_id.value.hex}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=False, timeout=30)
    settings = tmp_path / "claude-settings.json"
    settings.write_text(json.dumps({"model": "sonnet"}) + "\n", encoding="utf-8")
    command = ["claude", "--model", "sonnet", "--permission-mode", "default"]
    command += ["--settings", str(settings)]
    try:
        _tmux(
            socket, "new-session", "-d", "-s", f"ra-{session_id}", "-x", "160", "-y", "40",
            "-c", str(workspace), *command,
        )  # fmt: skip
        pane = _tmux(socket, "list-panes", "-t", f"=ra-{session_id}:", "-F", "#{pane_id}")
        pane_id = pane.stdout.strip()
        for option, value in (
            ("@remote_agents_schema", "2"),
            ("@remote_agents_id", str(session_id)),
            ("@remote_agents_project_id", "qualification"),
            ("@remote_agents_profile", "claude"),
            # As a managed pane is: an exited agent's pane stays, reading dead, so the stop's
            # success is observable as `preserved` rather than as a pane that vanished.
            ("remain-on-exit", "on"),
        ):
            _tmux(socket, "set-option", "-p", "-t", pane_id, option, value)
        open_to_composer(
            lambda: _tmux(socket, "capture-pane", "-p", "-t", pane_id).stdout,
            lambda key: (_tmux(socket, "send-keys", "-t", pane_id, key), time.sleep(0.5)),
            ready=CLAUDE_READY,
            interstitials=CLAUDE_OPENING,
            agent="claude",
        )
        terminal = TmuxTerminal(
            TmuxGateway(socket, AsyncTmuxRunner(), key_lock_directory=tmp_path / "locks"),
            {},
            {
                _CLAUDE: LaunchProfile(
                    "/usr/bin/claude",
                    ("/usr/bin/claude",),
                    {},
                    None,
                    # The curated profile's own keys, so a change there reaches this drill.
                    graceful_keys=next(
                        profile.graceful_keys
                        for profile in closed_profiles()
                        if profile.profile_id == _CLAUDE
                    ),
                )
            },
            startup_timeout=30.0,
            composers=profile_composers(),
            waits=TerminalWaits(prompt_settle=1.0, prompt_bound=45.0),
        )

        # 1. A real approval dialog: Claude asks before running a command that writes a file.
        _tmux(socket, "send-keys", "-t", pane_id, "-l", _ASK)
        time.sleep(0.5)
        _tmux(socket, "send-keys", "-t", pane_id, "Enter")
        _wait_for_state(socket, pane_id, PaneState.DIALOG, 120.0)

        # 2. Neither key sequence reaches it.
        toggled = asyncio.run(terminal.remote_control(session_id, RemoteControlState.ACTIVE))
        stopped = asyncio.run(terminal.graceful_stop(session_id, _CLAUDE))
        assert toggled is RemoteControlState.UNKNOWN, toggled
        assert (stopped.detail, stopped.live) == (AGENT_ASKING, True), stopped
        time.sleep(2.0)
        screen = _screen(socket, pane_id)
        assert classify(screen, profile_composers()["claude"]) is PaneState.DIALOG, (
            f"the dialog was answered by something:\n{screen}"
        )
        assert not (workspace / _MARKER).exists(), "the command ran: an Enter approved it"

        # 3. The drill declines the dialog itself, and the idle pane takes the stop.
        _tmux(socket, "send-keys", "-t", pane_id, "Escape")
        _wait_for_state(socket, pane_id, PaneState.IDLE, 60.0)
        exited = asyncio.run(terminal.graceful_stop(session_id, _CLAUDE))
        assert exited.preserved, exited
        assert not (workspace / _MARKER).exists()
    finally:
        _tmux(socket, "kill-server")


def test_a_real_cursor_agent_stop_gets_through_its_command_menu(tmp_path: Path) -> None:
    """cursor-agent's stop is `/quit Enter Enter`, and between the keys it draws its command menu
    over a trust box answered at launch -- which read as a trust dialog and stalled every such
    stop in 0.48.0. Here a real cursor-agent, in a workspace trusted a moment ago so the answered
    box is on screen, is stopped by the real `graceful_stop` and exits. No turn runs."""
    if shutil.which("cursor-agent") is None or shutil.which("tmux") is None:
        pytest.skip("BLOCKED: executable_missing: cursor-agent or tmux")
    status = subprocess.run(
        ["cursor-agent", "status"], capture_output=True, text=True, timeout=30, check=False
    )
    if "Logged in" not in status.stdout + status.stderr:
        pytest.skip("BLOCKED: cursor-agent is not logged in")
    session_id = SessionId.new()
    socket = f"remote-agents-test-keyed-{session_id.value.hex}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=False, timeout=30)
    cursor = ProfileId("cursor-agent")
    try:
        _tmux(
            socket, "new-session", "-d", "-s", f"ra-{session_id}", "-x", "160", "-y", "40",
            "-c", str(workspace), "cursor-agent",
        )  # fmt: skip
        pane_id = _tmux(socket, "list-panes", "-t", f"=ra-{session_id}:", "-F", "#{pane_id}")
        pane_id = pane_id.stdout.strip()
        for option, value in (
            ("@remote_agents_schema", "2"),
            ("@remote_agents_id", str(session_id)),
            ("@remote_agents_project_id", "qualification"),
            ("@remote_agents_profile", "cursor-agent"),
            ("remain-on-exit", "on"),
        ):
            _tmux(socket, "set-option", "-p", "-t", pane_id, option, value)
        deadline = time.monotonic() + 60.0
        answered = False
        screen = ""
        while time.monotonic() < deadline:
            screen = _tmux(socket, "capture-pane", "-p", "-t", pane_id).stdout
            if not answered and "Trust this workspace" in screen:
                _tmux(socket, "send-keys", "-t", pane_id, "a")
                answered = True
            if "Plan, search, build anything" in screen:
                break
            time.sleep(0.5)
        else:
            pytest.fail(f"cursor-agent never reached its composer:\n{screen}")
        terminal = TmuxTerminal(
            TmuxGateway(socket, AsyncTmuxRunner(), key_lock_directory=tmp_path / "locks"),
            {},
            {
                cursor: LaunchProfile(
                    "/usr/bin/cursor-agent",
                    ("/usr/bin/cursor-agent",),
                    {},
                    None,
                    graceful_keys=next(
                        profile.graceful_keys
                        for profile in closed_profiles()
                        if profile.profile_id == cursor
                    ),
                )
            },
            startup_timeout=30.0,
            composers=profile_composers(),
        )

        stopped = asyncio.run(terminal.graceful_stop(session_id, cursor))

        assert stopped.preserved, (stopped, _screen(socket, pane_id))
    finally:
        _tmux(socket, "kill-server")
