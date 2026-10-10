"""The git adapter against a real repository in `tmp_path`, on this host's git.

Each documented git behaviour the worktree feature leans on is re-checked here rather than
trusted from the manual: a worktree holding an untracked file is not removed, an unmerged
branch is not deleted, and ancestry flips only once a merge lands.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest

from remote_agents.adapters.git.checkout import GitCli
from remote_agents.ports.git_checkout import GitRefused


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _host_git_config_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host's global config may install hooks or demand signing into every new repo;
    neither the fixture's commits nor the adapter under test should depend on it."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "--template=", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "f.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    _git(root, "add", "f.txt")
    _git(root, "commit", "-q", "-m", "base")
    return root


async def test_top_level_and_head(repo: Path, tmp_path: Path) -> None:
    cli = GitCli()
    (repo / "sub").mkdir()
    assert await cli.top_level(repo / "sub") == repo.resolve()
    outside = tmp_path / "plain"
    outside.mkdir()
    assert await cli.top_level(outside) is None
    head = await cli.head(repo)
    assert head.branch == "main"
    assert head.commit == _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "--detach")
    assert (await cli.head(repo)).branch is None


async def test_add_worktree_creates_the_folder_and_the_branch(repo: Path, tmp_path: Path) -> None:
    cli = GitCli()
    tree = tmp_path / "wt"
    commit = _git(repo, "rev-parse", "HEAD")
    await cli.add_worktree(repo, tree, "ra/abcd1234", commit)
    assert (tree / "f.txt").is_file()
    assert _git(tree, "rev-parse", "--abbrev-ref", "HEAD") == "ra/abcd1234"
    assert _git(repo, "rev-parse", "ra/abcd1234") == commit
    with pytest.raises(GitRefused):
        await cli.add_worktree(repo, tmp_path / "wt2", "ra/abcd1234", commit)


async def test_remove_worktree_refuses_a_folder_with_an_untracked_file(
    repo: Path, tmp_path: Path
) -> None:
    cli = GitCli()
    tree = tmp_path / "wt"
    await cli.add_worktree(repo, tree, "ra/abcd1234", _git(repo, "rev-parse", "HEAD"))
    (tree / "new.txt").write_text("x", encoding="utf-8")
    assert "?? new.txt" in await cli.status_lines(tree)
    with pytest.raises(GitRefused):
        await cli.remove_worktree(repo, tree)
    assert (tree / "new.txt").exists()
    (tree / "new.txt").unlink()
    await cli.remove_worktree(repo, tree)
    assert not tree.exists()


async def test_delete_merged_branch_refuses_an_unmerged_branch_and_ancestry_follows_the_merge(
    repo: Path, tmp_path: Path
) -> None:
    cli = GitCli()
    tree = tmp_path / "wt"
    await cli.add_worktree(repo, tree, "ra/abcd1234", _git(repo, "rev-parse", "HEAD"))
    (tree / "g.txt").write_text("g\n", encoding="utf-8")
    _git(tree, "add", "g.txt")
    _git(tree, "commit", "-q", "-m", "work")
    await cli.remove_worktree(repo, tree)
    assert await cli.is_ancestor(repo, "ra/abcd1234", "main") is False
    with pytest.raises(GitRefused):
        await cli.delete_merged_branch(repo, "ra/abcd1234")
    assert _git(repo, "branch", "--list", "ra/abcd1234")
    _git(repo, "merge", "-q", "--ff-only", "ra/abcd1234")
    assert await cli.is_ancestor(repo, "ra/abcd1234", "main") is True
    await cli.delete_merged_branch(repo, "ra/abcd1234")
    assert _git(repo, "branch", "--list", "ra/abcd1234") == ""


async def test_diff_summary_counts_a_planted_change(repo: Path, tmp_path: Path) -> None:
    cli = GitCli()
    base = _git(repo, "rev-parse", "HEAD")
    tree = tmp_path / "wt"
    await cli.add_worktree(repo, tree, "ra/abcd1234", base)
    (tree / "f.txt").write_text("one\nTWO\nthree\nfour\nfive\n", encoding="utf-8")
    summary = await cli.diff_summary(tree, base)
    assert (summary.files, summary.added, summary.removed) == (1, 3, 1)
    assert summary.branch == "ra/abcd1234"
    assert summary.ahead == 0
    assert summary.dirty is True
    _git(tree, "commit", "-q", "-am", "edit")
    summary = await cli.diff_summary(tree, base)
    assert (summary.files, summary.added, summary.removed, summary.ahead) == (1, 3, 1, 1)
    assert summary.dirty is False


async def test_status_lists_every_kind_of_file_a_removal_would_touch(
    repo: Path, tmp_path: Path
) -> None:
    """The set `worktree remove` acts on, one member of each kind, each listed on its own.

    Ignored files are the members git deletes without asking, so a folder is only "clean"
    if this listing is: one entry per ignored file, inside ignored folders too.
    """
    (repo / ".gitignore").write_text("CLAUDE.md\n.claude/\nbuild/\n*.log\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    cli = GitCli()
    tree = tmp_path / "wt"
    await cli.add_worktree(repo, tree, "ra/abcd1234", _git(repo, "rev-parse", "HEAD"))
    (tree / "f.txt").write_text("changed\n", encoding="utf-8")
    (tree / "untracked.txt").write_text("u", encoding="utf-8")
    (tree / "CLAUDE.md").write_text("edited by the agent", encoding="utf-8")
    (tree / ".claude" / "deep").mkdir(parents=True)
    (tree / ".claude" / "deep" / "settings.local.json").write_text("{}", encoding="utf-8")
    (tree / "build").mkdir()
    (tree / "build" / "out.bin").write_bytes(b"x")
    (tree / "run.log").write_text("l", encoding="utf-8")
    (tree / "nested").mkdir()
    _git(tree / "nested", "init", "-q", "--template=")

    assert set(await cli.status_lines(tree)) == {
        " M f.txt",
        "?? untracked.txt",
        "?? nested/",
        "!! CLAUDE.md",
        "!! .claude/deep/settings.local.json",
        "!! build/out.bin",
        "!! run.log",
    }


async def test_git_removes_a_worktree_holding_only_ignored_files_without_asking(
    repo: Path, tmp_path: Path
) -> None:
    """Pins the git behaviour the listing above exists for (git 2.43.0): no `--force`, exit
    0, and an agent's edit to an ignored file is gone."""
    (repo / ".gitignore").write_text("CLAUDE.md\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    cli = GitCli()
    tree = tmp_path / "wt"
    await cli.add_worktree(repo, tree, "ra/abcd1234", _git(repo, "rev-parse", "HEAD"))
    (tree / "CLAUDE.md").write_text("edited by the agent", encoding="utf-8")
    assert await cli.status_lines(tree) == ("!! CLAUDE.md",)
    await cli.remove_worktree(repo, tree)
    assert not tree.exists()


async def test_a_hook_that_backgrounds_a_job_does_not_turn_a_finished_add_into_a_timeout(
    repo: Path, tmp_path: Path
) -> None:
    pid_file = tmp_path / "hook.pid"
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "post-checkout"
    hook.write_text(f'#!/bin/sh\nsleep 30 &\necho $! > "{pid_file}"\n', encoding="utf-8")
    hook.chmod(0o755)
    tree = tmp_path / "wt"
    started = time.monotonic()
    await GitCli(write_timeout=10).add_worktree(
        repo, tree, "ra/abcd1234", _git(repo, "rev-parse", "HEAD")
    )
    assert time.monotonic() - started < 5
    assert (tree / "f.txt").is_file()
    pid = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("the hook's background job outlived the call")


async def test_a_base_named_like_a_folder_or_shadowed_by_a_tag_still_resolves(
    repo: Path, tmp_path: Path
) -> None:
    cli = GitCli()
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "dev")
    (repo / "dev").mkdir()
    (repo / "dev" / "x.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "dev")
    _git(repo, "commit", "-q", "-m", "a folder named like the branch")
    summary = await cli.diff_summary(repo, "dev")
    assert (summary.files, summary.added, summary.ahead) == (1, 1, 1)

    _git(repo, "tag", "main", base)
    assert (await cli.head(repo)).branch == "main"
    head = _git(repo, "rev-parse", "HEAD")
    assert await cli.is_ancestor(repo, head, "refs/heads/main") is True
