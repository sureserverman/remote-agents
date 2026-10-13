"""Live drill: one Codex daemon, two managed panes -- each turn is filed under its own session.

Codex runs every turn in one app-server daemon per `CODEX_HOME`, and its hooks are spawned by
that daemon, so they inherit the environment of whichever pane started it. On 2026-10-13 that
filed an engineering-skills session's asks under an obsidian-wiki session that had ended four
days earlier (DEC-126). This opens the incident's shape for real: pane A starts the daemon, pane
B attaches to it, and a turn in B must drain as B's -- while the daemon itself still carries A.

`REMOTE_AGENTS_LIVE_ACCEPTANCE=1` is required, as it is for `test_codex_activity_hooks.py`: it
spends one short Codex turn on the owner's account. Every skip says `BLOCKED:` and why.

Boundaries: a `remote-agents-test-*` tmux socket that is destroyed; one disposable short
`CODEX_HOME` whose `auth.json` is a link to the owner's, never opened here, and which is retired
with its daemon in `finally`; a spool under `tmp_path`. The owner's `~/.codex/hooks.json` and the
service's production spool are never touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent_panes import CODEX_OPENING, CODEX_READY, open_to_composer
from codex_home import retire_codex_home, short_codex_home

from remote_agents.adapters.agents.hook_identity import FileHookRoster
from remote_agents.adapters.agents.registry import install_agent_hooks
from remote_agents.application.activity import drain_activity
from remote_agents.domain.models import SessionId
from remote_agents.ports.agent_activity import ActivityKind, AgentActivity
from remote_agents.ports.hook_roster import RosterSession
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE

_TURN = "Reply with exactly the word: spooled"


def _tmux(socket: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", socket, *arguments], capture_output=True, text=True, timeout=60, check=False
    )


def _open(socket: str, session_id: SessionId, workspace: Path, codex_home: Path) -> str:
    """A Codex pane carrying `session_id` as a managed pane would, opened to its composer."""
    _tmux(
        socket, "new-session", "-d", "-s", f"ra-{session_id}", "-x", "160", "-y", "40",
        "-c", str(workspace),
        "-e", f"{SESSION_ID_VARIABLE}={session_id}", "-e", f"CODEX_HOME={codex_home}",
        "codex",
    )  # fmt: skip
    pane = _tmux(socket, "list-panes", "-t", f"=ra-{session_id}:", "-F", "#{pane_id}").stdout
    pane = pane.strip()
    open_to_composer(
        lambda: _tmux(socket, "capture-pane", "-p", "-t", pane).stdout,
        lambda key: (_tmux(socket, "send-keys", "-t", pane, key), time.sleep(0.5)),
        ready=CODEX_READY,
        interstitials=CODEX_OPENING,
        agent="codex",
    )
    return pane


def _daemon_sessions(codex_home: Path) -> set[str]:
    """The session variable every app-server process running from `codex_home` carries."""
    found = set()
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            if not os.readlink(entry / "exe").startswith(f"{codex_home}/"):
                continue
            if b"app-server" not in (entry / "cmdline").read_bytes():
                continue
            environment = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        prefix = f"{SESSION_ID_VARIABLE}=".encode()
        found.update(
            item[len(prefix) :].decode() for item in environment if item.startswith(prefix)
        )
    return found


def _finished(spool: Path, seconds: float = 180.0) -> list[AgentActivity]:
    deadline = time.monotonic() + seconds
    drained: list[AgentActivity] = []
    while time.monotonic() < deadline:
        drained.extend(drain_activity(spool))
        if any(activity.kind is ActivityKind.COMPLETED for activity in drained):
            return drained
        time.sleep(1.0)
    pytest.fail(f"no finished turn was spooled in {seconds:.0f}s; drained {drained}")


def _written(path: Path, seconds: float = 10.0) -> str:
    """The copy a sibling Stop hook writes: Codex may still be running it when the record drains."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.is_file() and (text := path.read_text(encoding="utf-8")):
            return text
        time.sleep(0.2)
    pytest.fail(f"the payload-copying Stop hook wrote nothing to {path} in {seconds:.0f}s")


def test_a_turn_in_the_second_pane_is_filed_under_the_second_session(tmp_path: Path) -> None:
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")
    for needed in ("codex", "tmux"):
        if shutil.which(needed) is None:
            pytest.skip(f"BLOCKED: executable_missing: {needed}")
    if not (Path.home() / ".codex" / "auth.json").is_file():
        pytest.skip("BLOCKED: codex is not logged in with ChatGPT")

    socket = f"remote-agents-test-attribution-{SessionId.new().value.hex}"
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    workspaces = {name: tmp_path / name for name in ("a", "b")}
    for workspace in workspaces.values():
        workspace.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=False, timeout=30)
    first, second = SessionId.new(), SessionId.new()
    codex_home = short_codex_home()
    try:
        os.symlink(Path.home() / ".codex" / "auth.json", codex_home / "auth.json")
        (codex_home / "config.toml").write_text(
            'approval_policy = "on-request"\nsandbox_mode = "read-only"\n'
            "[notice]\nhide_rate_limit_model_nudge = true\n",
            encoding="utf-8",
        )
        hooks = codex_home / "hooks.json"
        install_agent_hooks(
            hooks, executable=Path(sys.executable), activity_directory=spool, provider="codex"
        )
        # A second Stop hook that keeps the raw payload, to pin where Codex puts `cwd`: the
        # hook recovers it from the payload's prefix when a turn's last message is too long.
        document = json.loads(hooks.read_text(encoding="utf-8"))
        payload_copy = tmp_path / "stop-payload.json"
        document["hooks"]["Stop"].append(
            {"hooks": [{"type": "command", "command": f"cat > {payload_copy}"}]}
        )
        hooks.write_text(json.dumps(document), encoding="utf-8")

        roster = FileHookRoster(spool)
        sessions = [RosterSession(str(first), workspaces["a"], datetime.now(UTC))]
        roster.write("codex", sessions)
        _open(socket, first, workspaces["a"], codex_home)
        sessions.append(RosterSession(str(second), workspaces["b"], datetime.now(UTC)))
        roster.write("codex", sessions)
        pane = _open(socket, second, workspaces["b"], codex_home)

        # The incident's precondition, re-measured rather than assumed: the daemon both panes
        # use carries the first pane's session, and only that one.
        assert _daemon_sessions(codex_home) == {str(first)}

        _tmux(socket, "send-keys", "-t", pane, "-l", _TURN)
        time.sleep(0.5)
        _tmux(socket, "send-keys", "-t", pane, "Enter")
        drained = _finished(spool)

        finished = [activity for activity in drained if activity.kind is ActivityKind.COMPLETED]
        assert [str(activity.session_id) for activity in finished] == [str(second)], drained
        raw = _written(payload_copy)
        assert raw.index('"cwd"') < raw.index('"last_assistant_message"'), raw
    finally:
        # The home is retired even when tmux does not answer: a daemon left running from it keeps
        # an authenticated Codex alive until the host reboots (BL-133).
        try:
            _tmux(socket, "kill-server")
        finally:
            retire_codex_home(codex_home)
