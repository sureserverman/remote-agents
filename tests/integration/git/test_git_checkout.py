"""The git adapter against a real repository in `tmp_path`, on this host's git.

Each documented git behaviour the worktree feature leans on is re-checked here rather than
trusted from the manual: a worktree holding an untracked file is not removed, an unmerged
branch is not deleted, and ancestry flips only once a merge lands.
"""

from __future__ import annotations

import subprocess
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
    assert await cli.status_lines(tree) == ("?? new.txt",)
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
