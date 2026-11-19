"""Keys sent to a managed pane that is in a tmux mode reach the agent, and never hang.

Found live on 2026-11-19 (v0.70.3): a rollover's stop typed `/exit` into a Claude pane that
was in view-mode, with the owner's console attached to the server. In a mode, keys go to the
mode's key table, not the agent, and `/` there opens a search prompt that `send-keys` waits on
for as long as a client is attached. The stop held the service's operation lock for 3.5 hours;
the bot answered nothing and no rollover ran. A pane gets into a mode through no fault of the
agent: the wheel enters copy-mode (the console turns `mouse` on), and a key binding whose shell
command prints leaves its output in view-mode.

Each send path is run on a real tmux server with one client attached, as on the owner's host,
under a time bound so the defect fails the test rather than hanging the suite.

Run alone: real-tmux tests fail spuriously beside another uv/pytest.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.domain.models import ProfileId, ProjectId, SessionId

PROJECT = ProjectId("opaque-editor")
PROFILE = ProfileId("fake")
#: Every send here takes well under a second; a hung `send-keys` never returns at all.
BOUND = 10.0

pytestmark = pytest.mark.skipif(shutil.which("script") is None, reason="needs util-linux script")


def _agent(tmp_path: Path) -> Path:
    agent = tmp_path / "echo_agent.py"
    agent.write_text(
        "import sys\n"
        "print('READY', flush=True)\n"
        "for line in sys.stdin:\n"
        "    print('GOT: ' + line.strip(), flush=True)\n",
        encoding="utf-8",
    )
    return agent


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.socket = f"remote-agents-test-{uuid4().hex}"
        self.gateway = TmuxGateway(
            self.socket, AsyncTmuxRunner(), intent_directory=tmp_path / "intents"
        )
        profile = LaunchProfile(
            sys.executable,
            (sys.executable, str(_agent(tmp_path))),
            {"PATH": os.environ["PATH"]},
            "READY",
        )
        self.terminal = TmuxTerminal(
            self.gateway, {PROJECT: tmp_path}, {PROFILE: profile}, startup_timeout=5.0
        )
        self.session_id = SessionId.new()
        self.client: subprocess.Popen[bytes] | None = None

    def tmux(self, *argv: str) -> str:
        return subprocess.run(
            ("tmux", "-L", self.socket, *argv), check=True, capture_output=True, text=True
        ).stdout

    async def launched(self) -> str:
        assert (await self.terminal.launch(self.session_id, PROJECT, PROFILE)).live
        (pane,) = self.tmux("list-panes", "-t", f"ra-{self.session_id}", "-F", "#{pane_id}").split()
        # One client attached somewhere on the server, as the owner's console always is.
        self.tmux("new-session", "-d", "-s", "elsewhere", "sleep 600")
        self.client = subprocess.Popen(
            ("script", "-qfc", f"tmux -L {self.socket} attach -t elsewhere", "/dev/null"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            if self.tmux("list-clients", "-F", "#{client_name}").strip():
                return pane
            await asyncio.sleep(0.1)
        raise AssertionError("no client attached")

    def close(self) -> None:
        if self.client is not None:
            self.client.kill()
            self.client.wait()
        subprocess.run(("tmux", "-L", self.socket, "kill-server"), capture_output=True)


@pytest.fixture
async def rig(tmp_path: Path):
    rig = Rig(tmp_path)
    try:
        yield rig
    finally:
        rig.close()


def _into_mode(rig: Rig, pane: str, mode: str) -> None:
    if mode == "view-mode":
        # A binding's shell command that prints: tmux shows its output over the pane.
        rig.tmux("run-shell", "-t", pane, "echo the hook said something")
    else:
        rig.tmux("copy-mode", "-t", pane)
    assert rig.tmux("display-message", "-p", "-t", pane, "#{pane_mode}").strip() == mode


def _sends(rig: Rig) -> dict[str, Callable[[], Awaitable[object]]]:
    return {
        "send_keys": lambda: rig.gateway.send_keys(rig.session_id, ("/exit", "Enter")),
        "send_keys_when": lambda: rig.gateway.send_keys_when(
            rig.session_id, ("/exit", "Enter"), lambda _capture: True
        ),
        "deliver_prompt": lambda: rig.gateway.deliver_prompt(
            rig.session_id,
            "/exit",
            may_paste=lambda _capture, _title: True,
            may_enter=lambda _capture: True,
            settle=0.2,
        ),
    }


@pytest.mark.parametrize("mode", ["view-mode", "copy-mode"])
@pytest.mark.parametrize("send", ["send_keys", "send_keys_when", "deliver_prompt"])
async def test_keys_into_a_pane_in_a_mode_reach_the_agent(rig: Rig, send: str, mode: str) -> None:
    pane = await rig.launched()
    _into_mode(rig, pane, mode)

    await asyncio.wait_for(_sends(rig)[send](), BOUND)

    assert rig.tmux("display-message", "-p", "-t", pane, "#{pane_in_mode}").strip() == "0"
    screen = ""
    for _ in range(30):
        screen = rig.tmux("capture-pane", "-p", "-t", pane)
        if "GOT: /exit" in screen:
            break
        await asyncio.sleep(0.1)
    assert "GOT: /exit" in screen, screen
