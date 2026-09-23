"""Live drill: the prompt relay types into real idle agent panes, and is refused by busy ones.

Everything about `send_prompt` is pinned at the argv level in `tests/unit/adapters/tmux/`, against
captures of real screens. What that cannot show is a real agent receiving the paste: that the
bracketed two-line prompt lands as **one** message, that one `Enter` submits it, and that a pane
mid-turn is refused before anything is typed. So this drives the real `TmuxTerminal` and gateway
against real Claude and Codex panes on a scratch tmux server.

**Not opt-in**, like `test_steady_panes.py` and for its reason: it is a stage gate's command, and an
opt-in skip there would report green over a drill that never ran. It skips only for what the host
lacks -- a binary or its credentials -- decided before a pane opens. It spends a few short turns
on the owner's accounts (Claude on `sonnet`).

Boundaries: a `remote-agents-test-*` socket that is destroyed; a disposable workspace per agent;
Codex in a disposable `CODEX_HOME` whose `auth.json` is a link to the owner's, never opened here;
Claude with the owner's own HOME, because a fresh config directory demands an interactive login.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from agent_panes import (
    CLAUDE_OPENING,
    CLAUDE_READY,
    CODEX_OPENING,
    CODEX_READY,
    Interstitial,
    open_to_composer,
)

from remote_agents.adapters.agents.registry import profile_composers
from remote_agents.adapters.tmux.composer import PaneState, classify
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TerminalWaits, TmuxTerminal
from remote_agents.domain.models import SessionId
from remote_agents.ports.terminal import PromptOutcome, PromptReason

_LINE_ONE = "Reply with exactly one word: relayed."
_LINE_TWO = "This second line belongs to the same message."
_LONG_TURN = "Count from 1 to 150, one number per line, and nothing else."


def _tmux(socket: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", socket, *arguments], capture_output=True, text=True, timeout=60, check=False
    )


def _requirements(agent: str) -> None:
    for needed in (agent, "tmux"):
        if shutil.which(needed) is None:
            pytest.skip(f"BLOCKED: executable_missing: {needed}")
    if agent == "claude" and not (Path.home() / ".claude" / ".credentials.json").is_file():
        pytest.skip("BLOCKED: claude is not logged in")
    if agent == "codex" and not (Path.home() / ".codex" / "auth.json").is_file():
        pytest.skip("BLOCKED: codex is not logged in with ChatGPT")


def _open_pane(agent: str, socket: str, workspace: Path) -> tuple[SessionId, list[str]]:
    """A real agent pane on the scratch server, marked as a managed session, at its composer."""
    command = ["claude", "--model", "sonnet"] if agent == "claude" else ["codex"]
    environment: list[str] = []
    if agent == "codex":
        codex_home = workspace / ".codex-home"
        codex_home.mkdir(mode=0o700)
        os.symlink(Path.home() / ".codex" / "auth.json", codex_home / "auth.json")
        (codex_home / "config.toml").write_text(
            'approval_policy = "on-request"\nsandbox_mode = "read-only"\n', encoding="utf-8"
        )
        environment = ["-e", f"CODEX_HOME={codex_home}"]
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=False, timeout=30)
    session_id = SessionId.new()
    _tmux(
        socket, "new-session", "-d", "-s", f"ra-{session_id}", "-x", "160", "-y", "40",
        "-c", str(workspace), *environment, *command,
    )  # fmt: skip
    pane = _tmux(socket, "list-panes", "-t", f"=ra-{session_id}:", "-F", "#{pane_id}").stdout
    pane = pane.strip()
    for option, value in (
        ("@remote_agents_schema", "2"),
        ("@remote_agents_id", str(session_id)),
        ("@remote_agents_project_id", "qualification"),
        ("@remote_agents_profile", agent),
    ):
        _tmux(socket, "set-option", "-p", "-t", pane, option, value)
    ready, opening = (
        (CLAUDE_READY, CLAUDE_OPENING) if agent == "claude" else (CODEX_READY, CODEX_OPENING)
    )
    _answer(socket, pane, agent, ready, opening)
    return session_id, [pane]


def _answer(
    socket: str,
    pane: str,
    agent: str,
    ready: str | tuple[str, ...],
    opening: tuple[Interstitial, ...],
) -> None:
    open_to_composer(
        lambda: _tmux(socket, "capture-pane", "-p", "-t", pane).stdout,
        lambda key: (_tmux(socket, "send-keys", "-t", pane, key), time.sleep(0.5)),
        ready=ready,
        interstitials=opening,
        agent=agent,
    )


def _terminal(socket: str, locks: Path) -> TmuxTerminal:
    return TmuxTerminal(
        TmuxGateway(socket, AsyncTmuxRunner(), key_lock_directory=locks),
        {},
        {},
        startup_timeout=1.0,
        composers=profile_composers(),
        waits=TerminalWaits(prompt_settle=1.0, prompt_bound=45.0),
    )


def _wait_for(socket: str, pane: str, needle: str, seconds: float = 120.0) -> str:
    deadline = time.monotonic() + seconds
    text = ""
    while time.monotonic() < deadline:
        text = _tmux(socket, "capture-pane", "-p", "-J", "-S", "-200", "-t", pane).stdout
        if needle in text:
            return text
        time.sleep(1.0)
    pytest.fail(f"{needle!r} never appeared in the pane:\n{text}")


def _wait_until_idle(socket: str, pane: str, agent: str, seconds: float = 120.0) -> None:
    descriptor = profile_composers()[agent]
    deadline = time.monotonic() + seconds
    text = ""
    while time.monotonic() < deadline:
        text = _tmux(socket, "capture-pane", "-p", "-t", pane).stdout
        if classify(text, descriptor) is PaneState.IDLE:
            return
        time.sleep(1.0)
    pytest.fail(f"{agent} never came back to an idle composer:\n{text}")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_runtime_types_into_an_idle_pane_and_is_refused_by_a_busy_one(
    agent: str, tmp_path: Path
) -> None:
    _requirements(agent)
    socket = f"remote-agents-test-relay-{SessionId.new().value.hex}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        session_id, (pane,) = _open_pane(agent, socket, workspace)
        terminal = _terminal(socket, tmp_path / "locks")

        # 1. Idle: a two-line message is delivered, and lands as one message.
        delivery = asyncio.run(terminal.send_prompt(session_id, f"{_LINE_ONE}\n{_LINE_TWO}"))
        assert delivery.outcome is PromptOutcome.SENT, (
            delivery,
            _tmux(socket, "capture-pane", "-p", "-t", pane).stdout,
        )
        screen = _wait_for(socket, pane, _LINE_TWO)
        lines = [line.strip() for line in screen.splitlines()]
        first = next(index for index, line in enumerate(lines) if _LINE_ONE in line)
        assert _LINE_TWO in lines[first + 1], (
            f"the two lines did not arrive as one message:\n{screen}"
        )
        assert lines.count(next(line for line in lines if _LINE_ONE in line)) == 1, (
            f"the message was submitted more than once:\n{screen}"
        )

        # 2. Busy: a long turn is started, and a message sent meanwhile is refused untyped.
        # First the reply to (1) must finish: read by the same classifier the relay uses, not
        # by a word that the prompt itself contains.
        _wait_until_idle(socket, pane, agent)
        started = asyncio.run(terminal.send_prompt(session_id, _LONG_TURN))
        assert started.outcome is PromptOutcome.SENT, started
        refused = asyncio.run(terminal.send_prompt(session_id, "This must never be typed."))
        assert (refused.outcome, refused.reason) == (PromptOutcome.REFUSED, PromptReason.BUSY), (
            refused,
            _tmux(socket, "capture-pane", "-p", "-t", pane).stdout,
        )
        after = _tmux(socket, "capture-pane", "-p", "-J", "-S", "-400", "-t", pane).stdout
        assert "This must never be typed." not in after
    finally:
        _tmux(socket, "kill-server")
