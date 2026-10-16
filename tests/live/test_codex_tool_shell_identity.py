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

The facts the design relies on are asserted (`-k measure`): each shell's `CODEX_THREAD_ID` is its
own pane's thread, and the two differ. The wrong session variable is recorded here, not asserted:
the design never reads it, so a later Codex that fixes it must not fail this drill.

The same tool command then runs the installed `remote-agents session-id` against a roster the
drill publishes, as `serve` would (`-k verb`, DEC-128): A and B each print their own id, and a
Codex the owner starts by hand in the project's main checkout -- sharing the daemon, so its shell
carries A's variable -- exits 3. A and B work in the project's worktrees, as managed sessions do.
What the verb cannot tell apart is `hook_identity`'s recorded limit, not drilled here: a Codex
started by hand in the very folder a live managed session works in is filed under that session.

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
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent_panes import CODEX_OPENING, CODEX_READY, open_to_composer
from codex_home import retire_codex_home, short_codex_home

from remote_agents.adapters.agents.hook_identity import FileHookRoster
from remote_agents.domain.models import SessionId
from remote_agents.ports.hook_roster import RosterSession
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE

#: The installed console script, not the module: the writer runs the command a shell would.
_VERB = Path(sys.executable).parent / "remote-agents"


def _probe_script(spool: Path) -> str:
    """What a pane's tool command runs: one `name=value` line per fact, then `done`."""
    return f"""#!/bin/sh
{{
  printf 'session=%s\\n' "${{{SESSION_ID_VARIABLE}-}}"
  printf 'thread=%s\\n' "${{CODEX_THREAD_ID-}}"
  printf 'verb=%s\\n' "$('{_VERB}' session-id --activity-dir '{spool}' 2>/dev/null)"
  '{_VERB}' session-id --activity-dir '{spool}' >/dev/null 2>&1
  printf 'verb_exit=%s\\n' "$?"
  printf 'done=1\\n'
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
        if written.is_file() and "done=1" in (text := written.read_text(encoding="utf-8")):
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


@dataclass(frozen=True, slots=True)
class Drill:
    """One tmux socket and one daemon home, a project with two worktrees, and the roster's spool."""

    socket: str
    codex_home: Path
    project: Path
    spool: Path
    roster: FileHookRoster
    sessions: list[RosterSession]


def _git(folder: Path, *arguments: str) -> None:
    """Git in the drill's throwaway project, with no hook of the owner's run on it."""
    identity = ("-c", "user.name=drill", "-c", "user.email=drill@invalid")
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *identity, *arguments],
        cwd=folder, check=True, timeout=30, capture_output=True,
    )  # fmt: skip


@pytest.fixture(scope="module")
def drill(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Drill]:
    """The shared ground every pane opens on. Module-scoped: the turns cost quota."""
    if os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") != "1":
        pytest.skip("BLOCKED: REMOTE_AGENTS_LIVE_ACCEPTANCE is not enabled")
    for needed in ("codex", "tmux", "git"):
        if shutil.which(needed) is None:
            pytest.skip(f"BLOCKED: executable_missing: {needed}")
    if not (Path.home() / ".codex" / "auth.json").is_file():
        pytest.skip("BLOCKED: codex is not logged in with ChatGPT")
    if not _VERB.is_file():
        pytest.skip(f"BLOCKED: no installed entry point at {_VERB}")

    root = tmp_path_factory.mktemp("tool-shell")
    spool = root / "spool"
    spool.mkdir(mode=0o700)
    project = root / "project"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "commit", "-q", "--allow-empty", "-m", "start")
    for name in ("a", "b"):
        _git(project, "worktree", "add", "-q", "-b", name, str(root / name))
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
        yield Drill(socket, codex_home, project, spool, FileHookRoster(spool), [])
    finally:
        # Retired even when tmux does not answer: a daemon left running from the home keeps an
        # authenticated Codex alive until the host reboots (BL-133).
        try:
            _tmux(socket, "kill-server")
        finally:
            retire_codex_home(codex_home)


def _probed(drill: Drill, workspace: Path, session: str | None) -> Pane:
    """A Codex pane in `workspace` -- managed when given a session -- after its probe ran."""
    (workspace / "probe.sh").write_text(_probe_script(drill.spool), encoding="utf-8")
    if session is not None:
        # Published before the pane opens, as `serve` lists a session once its record exists.
        drill.sessions.append(RosterSession(session, workspace, datetime.now(UTC)))
        drill.roster.write("codex", drill.sessions)
    name = f"ra-{session}" if session is not None else f"by-hand-{SessionId.new().value.hex}"
    pane = _open(drill.socket, name, workspace, drill.codex_home, session)
    seen = _probe(drill.socket, pane, workspace)
    return Pane(session or "", workspace, seen, _thread(drill.codex_home, workspace))


@pytest.fixture(scope="module")
def panes(drill: Drill) -> tuple[Pane, Pane]:
    """Managed panes A then B, in the project's two worktrees, on one daemon: A starts it."""
    root = drill.project.parent
    first = _probed(drill, root / "a", str(SessionId.new()))
    second = _probed(drill, root / "b", str(SessionId.new()))
    return first, second


@pytest.fixture(scope="module")
def by_hand(drill: Drill, panes: tuple[Pane, Pane]) -> Pane:
    """A Codex started by hand in the project's main checkout, attached to A's daemon."""
    return _probed(drill, drill.project, None)


def test_measure_each_tool_shell_is_told_its_own_thread(panes: tuple[Pane, Pane]) -> None:
    first, second = panes
    assert first.seen["thread"] == first.thread, first
    assert second.seen["thread"] == second.thread, second
    assert first.thread != second.thread


def test_verb_each_managed_pane_prints_its_own_session(panes: tuple[Pane, Pane]) -> None:
    for pane in panes:
        assert (pane.seen["verb_exit"], pane.seen["verb"]) == ("0", pane.session_id), pane


def test_verb_a_codex_started_by_hand_exits_3(by_hand: Pane, panes: tuple[Pane, Pane]) -> None:
    # Its shell carries A's variable, inherited from the daemon A started: never evidence.
    assert by_hand.seen["session"] == panes[0].session_id, by_hand
    assert (by_hand.seen["verb_exit"], by_hand.seen["verb"]) == ("3", ""), by_hand
