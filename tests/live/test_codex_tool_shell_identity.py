"""Live drill: one Codex daemon, two managed panes -- what each pane's tool shell is told.

A handoff writer runs as a tool command of the agent, and stamps the session it belongs to.
DEC-126 measured that Codex hooks run in the per-home app-server daemon and carry the environment
of whichever pane started it. Whether tool shells do the same was inferred, not measured; this
drill measures it. Pane A starts the daemon, pane B attaches to it, and each runs one trivial
tool command that writes what its shell sees.

Measured 2026-10-16 on codex-cli 0.162.1, pane A launched with session `ee63d5b9-…`, pane B
with `2683fdd6-…`:

| Shell | `REMOTE_AGENTS_SESSION_ID` | `CODEX_THREAD_ID` |
|---|---|---|
| A's tool command | `ee63d5b9-…` (A's, right) | `01a14387-f8c4-…` (A's thread) |
| B's tool command | `ee63d5b9-…` (**A's, wrong**) | `01a14388-1263-…` (B's thread) |

So a tool shell is spawned by the daemon as a hook is, and inherits the environment of the pane
that started it: B's shell names A's session. Only `CODEX_THREAD_ID` is per thread.

The facts the design relies on are asserted: each shell's `CODEX_THREAD_ID` is its own pane's
thread, and the two differ. The wrong session variable is recorded here, not asserted: the design
never reads it, so a later Codex that fixes it must not fail this drill.

`REMOTE_AGENTS_LIVE_ACCEPTANCE=1` is required: it spends short Codex turns on the owner's
account. Every skip says `BLOCKED:` and why.

Boundaries: a `remote-agents-test-*` tmux socket that is destroyed; one disposable short
`CODEX_HOME` whose `auth.json` is a link to the owner's, never opened here, and which is retired
with its daemon in `finally`; workspaces under pytest's temporary directory. The owner's
`~/.codex` configuration and the service's production spool are never touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from agent_panes import CODEX_OPENING, CODEX_READY, open_to_composer
from codex_home import retire_codex_home, short_codex_home

from remote_agents.domain.models import SessionId
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE

#: What each pane's tool command writes: one `name=value` line per fact, read back by the test.
_PROBE = f"""#!/bin/sh
{{
  printf 'session=%s\\n' "${{{SESSION_ID_VARIABLE}-}}"
  printf 'thread=%s\\n' "${{CODEX_THREAD_ID-}}"
}} > seen.txt
"""

_TURN = "Run the shell command `sh probe.sh` exactly once, then reply with exactly the word: done"


def _tmux(socket: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", socket, *arguments], capture_output=True, text=True, timeout=60, check=False
    )


@dataclass(frozen=True, slots=True)
class Pane:
    """One Codex pane of the drill, and what its tool shell reported."""

    session_id: str
    workspace: Path
    seen: dict[str, str]
    thread: str
    """The thread this pane's turn ran in, as its rollout's `session_meta` names it."""


def _open(socket: str, name: str, workspace: Path, codex_home: Path, session: str | None) -> str:
    """A Codex pane, carrying `session` as a managed pane would when given, at its composer."""
    environment = ["-e", f"CODEX_HOME={codex_home}"]
    if session is not None:
        environment += ["-e", f"{SESSION_ID_VARIABLE}={session}"]
    _tmux(
        socket, "new-session", "-d", "-s", name, "-x", "160", "-y", "40",
        "-c", str(workspace), *environment, "codex",
    )  # fmt: skip
    pane = _tmux(socket, "list-panes", "-t", f"={name}:", "-F", "#{pane_id}").stdout.strip()
    open_to_composer(
        lambda: _tmux(socket, "capture-pane", "-p", "-t", pane).stdout,
        lambda key: (_tmux(socket, "send-keys", "-t", pane, key), time.sleep(0.5)),
        ready=CODEX_READY,
        interstitials=CODEX_OPENING,
        agent="codex",
    )
    return pane


def _probe(socket: str, pane: str, workspace: Path, seconds: float = 180.0) -> dict[str, str]:
    """Have the pane's agent run the probe once; what its shell wrote, read back."""
    _tmux(socket, "send-keys", "-t", pane, "-l", _TURN)
    time.sleep(0.5)
    _tmux(socket, "send-keys", "-t", pane, "Enter")
    written = workspace / "seen.txt"
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if written.is_file() and (text := written.read_text(encoding="utf-8")).count("\n") >= 2:
            return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        time.sleep(1.0)
    screen = _tmux(socket, "capture-pane", "-p", "-t", pane).stdout
    pytest.fail(f"the probe wrote nothing in {seconds:.0f}s; the pane shows:\n{screen}")


def _thread(codex_home: Path, workspace: Path, seconds: float = 30.0) -> str:
    """The id of the thread whose rollout ran in `workspace`."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for rollout in (codex_home / "sessions").glob("*/*/*/rollout-*.jsonl"):
            try:
                first = json.loads(rollout.read_text(encoding="utf-8").splitlines()[0])
            except (OSError, IndexError, ValueError):
                continue
            meta = first.get("payload", {})
            if first.get("type") == "session_meta" and meta.get("cwd") == str(workspace):
                return str(meta["id"])
        time.sleep(0.5)
    pytest.fail(f"no rollout under {codex_home} ran in {workspace}")


@pytest.fixture(scope="module")
def panes(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Pane, Pane]]:
    """Panes A then B on one daemon, each probed once. Module-scoped: the turns cost quota."""
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")
    for needed in ("codex", "tmux"):
        if shutil.which(needed) is None:
            pytest.skip(f"BLOCKED: executable_missing: {needed}")
    if not (Path.home() / ".codex" / "auth.json").is_file():
        pytest.skip("BLOCKED: codex is not logged in with ChatGPT")

    root = tmp_path_factory.mktemp("tool-shell")
    socket = f"remote-agents-test-tool-shell-{SessionId.new().value.hex}"
    codex_home = short_codex_home()
    try:
        os.symlink(Path.home() / ".codex" / "auth.json", codex_home / "auth.json")
        (codex_home / "config.toml").write_text(
            # The probe writes into its own folder, and nothing may stop to ask.
            'approval_policy = "never"\nsandbox_mode = "workspace-write"\n'
            "[notice]\nhide_rate_limit_model_nudge = true\n",
            encoding="utf-8",
        )
        found = []
        for name in ("a", "b"):
            workspace = root / name
            workspace.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=False, timeout=30)
            (workspace / "probe.sh").write_text(_PROBE, encoding="utf-8")
            session = str(SessionId.new())
            pane = _open(socket, f"ra-{session}", workspace, codex_home, session)
            seen = _probe(socket, pane, workspace)
            found.append(Pane(session, workspace, seen, _thread(codex_home, workspace)))
        yield found[0], found[1]
    finally:
        # Retired even when tmux does not answer: a daemon left running from the home keeps an
        # authenticated Codex alive until the host reboots (BL-133).
        try:
            _tmux(socket, "kill-server")
        finally:
            retire_codex_home(codex_home)


def test_measure_each_tool_shell_is_told_its_own_thread(panes: tuple[Pane, Pane]) -> None:
    first, second = panes
    assert first.seen["thread"] == first.thread, first
    assert second.seen["thread"] == second.thread, second
    assert first.thread != second.thread
