"""Opt-in: a real worktree launch inherits Codex's trust and makes Cursor ask (BL-116, DEC-016).

`docs/acceptance-2026-10-09-worktree-trust.md` measured it once by hand; this re-measures the
two that matter on each run it is selected for. Each selected profile gets a worktree of a
repository that agent already trusts -- Codex: this repository; Cursor: one whose
`~/.cursor/projects/<…>/.workspace-trusted` names it, found at run time -- launched through
`TmuxTerminal` with the worktree as its folder, on a scratch tmux server.

Nothing is answered and no model request is made: the screen is read at the settle, the server
killed, the worktree removed with `git worktree remove` and its branch with `branch -d` (both
unforced), and the three trust stores checked for an entry naming the worktree.

Set `REMOTE_AGENTS_WORKTREE_REPORT` to a folder to have each launch's observation and settled
screen written there, one JSON file per profile.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import profile_trust_dialogs
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import build_launch_profile
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.domain.models import ProjectId, SessionId
from remote_agents.domain.profiles import closed_profiles

REPO = Path(__file__).resolve().parents[2]
HOME = Path.home()

#: What each agent must do in a worktree of a repository it already trusts.
EXPECTED = {
    "codex": {"asks": False, "dialog": "Folder access"},
    "cursor-agent": {"asks": True, "dialog": "Workspace Trust Required"},
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, timeout=60
    ).stdout.strip()


def _cursor_trusted_repository() -> Path | None:
    """A git repository Cursor has already trusted, read off its own trust markers."""
    for marker in sorted((HOME / ".cursor" / "projects").glob("*/.workspace-trusted")):
        try:
            path = Path(json.loads(marker.read_text(encoding="utf-8"))["workspacePath"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not path.is_dir() or path.is_relative_to(Path("/tmp")):
            continue
        try:
            top = _git(path, "rev-parse", "--show-toplevel")
        except subprocess.CalledProcessError:
            continue
        if Path(top).resolve() == path.resolve():
            return path.resolve()
    return None


def _trusted_repository(profile_id: str) -> Path | None:
    return REPO if profile_id == "codex" else _cursor_trusted_repository()


def _trust_entries(worktree: Path) -> list[str]:
    """Every trust store that now names the worktree."""
    found = []
    for store in (HOME / ".codex" / "config.toml", HOME / ".claude.json"):
        try:
            if str(worktree) in store.read_text(encoding="utf-8"):
                found.append(str(store))
        except OSError:
            continue
    for marker in (HOME / ".cursor" / "projects").glob("*/.workspace-trusted"):
        try:
            if str(worktree) in marker.read_text(encoding="utf-8"):
                found.append(str(marker))
        except OSError:
            continue
    return found


@pytest.mark.live_profile
@pytest.mark.parametrize("profile_id", sorted(EXPECTED))
async def test_a_worktree_launch_inherits_or_asks_for_trust_as_measured(
    tmp_path: Path, request: pytest.FixtureRequest, profile_id: str
) -> None:
    if profile_id not in request.config.getoption("profile"):
        pytest.skip(f"BLOCKED: {profile_id}_profile_not_selected")
    definition = next(p for p in closed_profiles() if str(p.profile_id) == profile_id)
    executable = shutil.which(definition.executable)
    if executable is None:
        pytest.skip("BLOCKED: executable_missing")
    repository = _trusted_repository(profile_id)
    if repository is None:
        pytest.skip(f"BLOCKED: no repository {profile_id} already trusts")

    session_id = SessionId.new()
    branch = f"ra/{str(session_id)[:8]}"
    worktree = (tmp_path / "worktrees" / str(session_id)).resolve()
    worktree.parent.mkdir(parents=True)
    environment = {
        key: os.environ[key]
        for key in ("HOME", "LANG", "LC_ALL", "PATH", "TERM")
        if key in os.environ
    }
    profile = build_launch_profile(definition, Path(executable), session_id, environment)
    project_id = ProjectId("trusted")
    socket = f"remote-agents-test-{session_id.value.hex}"

    async def folder_for(_session: SessionId, _project: ProjectId) -> Path:
        return worktree

    screen = ""
    try:
        _git(repository, "worktree", "add", "-q", "-b", branch, str(worktree), "HEAD")
        subprocess.run(
            ("tmux", "-L", socket, "-f", "/dev/null", "new-session", "-d", "-s", "hold")
            + ("-x", "160", "-y", "40", ";", "set-option", "-g", "default-size", "160x40"),
            check=True,
        )
        gateway = TmuxGateway(socket, AsyncTmuxRunner(), intent_directory=tmp_path / "intents")
        terminal = TmuxTerminal(
            gateway,
            {project_id: repository},
            {definition.profile_id: profile},
            startup_timeout=30,
            trust_dialogs=profile_trust_dialogs(),
            folder_for=folder_for,
        )
        launched = await terminal.launch(session_id, project_id, definition.profile_id)
        screen = await gateway.capture(session_id)
    finally:
        subprocess.run(("tmux", "-L", socket, "kill-server"), check=False)
        removed = subprocess.run(
            ("git", "-C", str(repository), "worktree", "remove", str(worktree)),
            capture_output=True,
            text=True,
        )
        deleted = subprocess.run(
            ("git", "-C", str(repository), "branch", "-d", branch), capture_output=True, text=True
        )
        if removed.returncode or deleted.returncode:
            # Said even when an earlier failure is propagating, so a leftover in an owner's
            # repository is never silent.
            print(
                f"LEFTOVER in {repository}: worktree remove -> {removed.stderr.strip()!r}; "
                f"branch -d {branch} -> {deleted.stderr.strip()!r}"
            )

    report = os.environ.get("REMOTE_AGENTS_WORKTREE_REPORT")
    if report:
        Path(report, f"{profile_id}.json").write_text(
            json.dumps(
                {
                    "version": subprocess.run(
                        (executable, "--version"), capture_output=True, text=True
                    ).stdout.strip(),
                    "repository": str(repository),
                    "worktree": str(worktree),
                    "live": launched.live,
                    "awaiting_trust": launched.awaiting_trust,
                    "detail": launched.detail,
                    "screen": screen,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    expected = EXPECTED[profile_id]
    assert launched.live, (launched, screen)
    assert launched.awaiting_trust is expected["asks"], screen
    assert (expected["dialog"] in screen) is expected["asks"], screen
    assert removed.returncode == 0, removed.stderr
    assert deleted.returncode == 0, deleted.stderr
    assert _trust_entries(worktree) == []
