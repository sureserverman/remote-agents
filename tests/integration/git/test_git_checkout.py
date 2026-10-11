"""The git adapter against a real repository in `tmp_path`, on this host's git.

Each documented git behaviour the worktree feature leans on is re-checked here rather than
trusted from the manual: a worktree holding an untracked file is not removed, an unmerged
branch is not deleted, and ancestry flips only once a merge lands.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from remote_agents.adapters.git.checkout import GitCli
from remote_agents.ports.git_checkout import GitHead, GitRefused


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _host_git_config_is_not_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The host's global config may install hooks or demand signing into every new repo;
    neither the fixture's commits nor the adapter under test should depend on it."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # The adapter drops every inherited `GIT_*` variable before git runs, so the two above
    # reach only the test's own `_git`; a home of its own keeps the host's config from both.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))


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
    assert not await cli.has_branch(repo, "ra/abcd1234")
    await cli.add_worktree(repo, tree, "ra/abcd1234", commit)
    assert await cli.has_branch(repo, "ra/abcd1234")
    assert (tree / "f.txt").is_file()
    assert _git(tree, "rev-parse", "--abbrev-ref", "HEAD") == "ra/abcd1234"
    assert _git(repo, "rev-parse", "ra/abcd1234") == commit
    with pytest.raises(GitRefused):
        await cli.add_worktree(repo, tmp_path / "wt2", "ra/abcd1234", commit)


async def test_worktree_head_reads_git_s_entry_for_a_folder_present_detached_or_gone(
    repo: Path, tmp_path: Path
) -> None:
    cli = GitCli()
    tree = tmp_path / "w t"
    commit = _git(repo, "rev-parse", "HEAD")
    assert await cli.worktree_head(repo, tree) is None
    await cli.add_worktree(repo, tree, "ra/abcd1234", commit)
    assert await cli.worktree_head(repo, tree) == GitHead(commit, "ra/abcd1234")
    _git(tree, "checkout", "-q", "--detach")
    _git(tree, "commit", "-q", "--allow-empty", "-m", "reachable only from this HEAD")
    detached = _git(tree, "rev-parse", "HEAD")
    assert await cli.worktree_head(repo, tree) == GitHead(detached, None)
    shutil.rmtree(tree)
    assert await cli.worktree_head(repo, tree) == GitHead(detached, None)
    assert (await cli.worktree_head(repo, repo)).branch == "main"


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
        # git names the nested repository; the walk names its `.git` too.
        "G? nested/.git",
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


async def test_an_edit_git_was_told_to_hide_is_still_listed(repo: Path, tmp_path: Path) -> None:
    """The same class as an ignored file: `assume-unchanged` and `skip-worktree` hide an edit
    from `status`, and `worktree remove` deletes it without asking (git 2.43.0)."""
    (repo / "g.txt").write_text("g\n", encoding="utf-8")
    _git(repo, "add", "g.txt")
    _git(repo, "commit", "-q", "-m", "g")
    cli = GitCli()
    tree = tmp_path / "wt"
    await cli.add_worktree(repo, tree, "ra/abcd1234", _git(repo, "rev-parse", "HEAD"))
    _git(tree, "update-index", "--assume-unchanged", "f.txt")
    _git(tree, "update-index", "--skip-worktree", "g.txt")
    (tree / "f.txt").write_text("hidden edit\n", encoding="utf-8")
    (tree / "g.txt").write_text("hidden edit\n", encoding="utf-8")
    assert _git(tree, "status", "--porcelain") == ""

    assert set(await cli.status_lines(tree)) == {"h? f.txt", "S? g.txt"}


async def test_an_entry_named_git_that_status_skips_is_still_listed(
    repo: Path, tmp_path: Path
) -> None:
    """git does not list an entry named `.git` it cannot open as a repository -- a file, a
    plain folder -- yet `worktree remove` deletes it. Measured on git 2.43.0."""
    (repo / ".gitignore").write_text("ign/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    cli = GitCli()
    tree = tmp_path / "wt"
    await cli.add_worktree(repo, tree, "ra/abcd1234", _git(repo, "rev-parse", "HEAD"))
    (tree / "sub").mkdir()
    (tree / "sub" / ".git").write_text("not a gitfile\n", encoding="utf-8")
    (tree / "sub2" / ".git").mkdir(parents=True)
    (tree / "sub2" / ".git" / "data").write_text("work\n", encoding="utf-8")
    (tree / "ign" / "sub3" / ".GIT").mkdir(parents=True)
    os.symlink(tree / "sub2", tree / "link")

    assert set(await cli.status_lines(tree)) == {
        "G? sub/.git",
        "G? sub2/.git",
        "G? ign/sub3/.GIT",
        "?? link",
    }


async def test_unmerged_history_counts_commits_only_the_worktree_s_refs_and_reflogs_reach(
    repo: Path, tmp_path: Path
) -> None:
    cli = GitCli()
    tree = tmp_path / "wt"
    await cli.add_worktree(repo, tree, "ra/abcd1234", _git(repo, "rev-parse", "HEAD"))
    assert await cli.unmerged_history(repo, tree, "ra/abcd1234", "main") == 0

    _git(tree, "commit", "-q", "--allow-empty", "-m", "made, then reset away")
    _git(tree, "reset", "-q", "--hard", "HEAD~1")
    assert await cli.unmerged_history(repo, tree, "ra/abcd1234", "main") == 1

    _git(tree, "commit", "-q", "--allow-empty", "-m", "kept by a per-worktree ref only")
    kept = _git(tree, "rev-parse", "HEAD")
    _git(tree, "update-ref", "refs/bisect/bad", kept)
    shutil.rmtree(tree)
    # The folder is gone; git's entry, its refs and its reflog still name both commits.
    assert await cli.unmerged_history(repo, tree, "ra/abcd1234", "main") == 2

    _git(repo, "merge", "-q", "--ff-only", kept)
    assert await cli.unmerged_history(repo, tree, "ra/abcd1234", "main") == 1


async def test_each_source_of_unmerged_history_is_read_on_its_own(
    repo: Path, tmp_path: Path
) -> None:
    """A commit no reflog names, held by a per-worktree ref alone; and one the branch's own
    reflog alone names, moved there and back from the main checkout."""
    cli = GitCli()
    tree = tmp_path / "wt"
    base = _git(repo, "rev-parse", "HEAD")
    await cli.add_worktree(repo, tree, "ra/abcd1234", base)

    orphan = _git(tree, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "ref only")
    _git(tree, "update-ref", "refs/worktree/save", orphan)
    assert await cli.unmerged_history(repo, tree, "ra/abcd1234", "main") == 1
    _git(tree, "update-ref", "-d", "refs/worktree/save")
    assert await cli.unmerged_history(repo, tree, "ra/abcd1234", "main") == 0

    stray = _git(repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "branch reflog only")
    _git(repo, "update-ref", "-m", "moved", "refs/heads/ra/abcd1234", stray)
    _git(repo, "update-ref", "-m", "back", "refs/heads/ra/abcd1234", base)
    assert await cli.unmerged_history(repo, tree, "ra/abcd1234", "main") == 1


async def test_with_reflogs_off_orig_head_still_counts_a_commit_reset_away(
    repo: Path, tmp_path: Path
) -> None:
    _git(repo, "config", "core.logAllRefUpdates", "false")
    cli = GitCli()
    tree = tmp_path / "wt"
    await cli.add_worktree(repo, tree, "ra/abcd1234", _git(repo, "rev-parse", "HEAD"))
    _git(tree, "commit", "-q", "--allow-empty", "-m", "made, then reset away")
    _git(tree, "reset", "-q", "--hard", "HEAD~1")

    assert await cli.unmerged_history(repo, tree, "ra/abcd1234", "main") == 1


@pytest.mark.parametrize(
    "holder",
    [
        "refs/worktree/save",
        "refs/bisect/bad",
        "refs/rewritten/onto",
        "ORIG_HEAD",
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "REBASE_HEAD",
        "BISECT_HEAD",
        "AUTO_MERGE",
        "rebase-merge/orig-head",
        "rebase-apply/orig-head",
    ],
)
async def test_every_per_worktree_holder_of_a_commit_is_read(
    repo: Path, tmp_path: Path, holder: str
) -> None:
    """Each holder alone, naming a commit no reflog and no branch reaches."""
    cli = GitCli()
    tree = tmp_path / "wt"
    await cli.add_worktree(repo, tree, "ra/abcd1234", _git(repo, "rev-parse", "HEAD"))
    orphan = _git(tree, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "held once")
    if holder.startswith("refs/"):
        _git(tree, "update-ref", holder, orphan)
    else:
        admin = Path(_git(tree, "rev-parse", "--absolute-git-dir"))
        (admin / holder).parent.mkdir(parents=True, exist_ok=True)
        (admin / holder).write_text(orphan + "\n", encoding="ascii")

    assert await cli.unmerged_history(repo, tree, "ra/abcd1234", "main") == 1


async def test_a_worktree_reached_through_a_symlinked_parent_is_still_found(
    repo: Path, tmp_path: Path
) -> None:
    """git records the real path; the folder is asked by the linked one."""
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    cli = GitCli()
    tree = tmp_path / "link" / "wt"
    await cli.add_worktree(repo, tree, "ra/abcd1234", _git(repo, "rev-parse", "HEAD"))
    orphan = _git(tree, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "ref only")
    _git(tree, "update-ref", "refs/worktree/save", orphan)

    assert await cli.unmerged_history(repo, tree, "ra/abcd1234", "main") == 1


async def test_an_entry_whose_admin_folder_cannot_be_placed_is_no_answer(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from remote_agents.adapters.git import checkout

    cli = GitCli()
    tree = tmp_path / "wt"
    await cli.add_worktree(repo, tree, "ra/abcd1234", _git(repo, "rev-parse", "HEAD"))
    monkeypatch.setattr(checkout, "_admin_for", lambda common, path: None)

    with pytest.raises(GitRefused):
        await cli.unmerged_history(repo, tree, "ra/abcd1234", "main")


async def test_a_reflog_git_cannot_read_for_an_existing_ref_is_no_answer(
    repo: Path, tmp_path: Path
) -> None:
    cli = GitCli()
    tree = tmp_path / "wt"
    await cli.add_worktree(repo, tree, "ra/abcd1234", _git(repo, "rev-parse", "HEAD"))
    admin = Path(_git(tree, "rev-parse", "--absolute-git-dir"))
    (admin / "HEAD").write_text("not a ref at all\n", encoding="ascii")

    with pytest.raises(GitRefused):
        await cli.unmerged_history(repo, tree, "ra/abcd1234", "main")


async def test_a_folder_the_walk_cannot_read_is_no_answer(repo: Path, tmp_path: Path) -> None:
    cli = GitCli()
    tree = tmp_path / "wt"
    await cli.add_worktree(repo, tree, "ra/abcd1234", _git(repo, "rev-parse", "HEAD"))
    locked = tree / "locked"
    (locked / "inner").mkdir(parents=True)
    locked.chmod(0o000)
    try:
        with pytest.raises(GitRefused):
            await cli.status_lines(tree)
    finally:
        locked.chmod(0o700)


async def test_a_relative_gitdir_is_placed_against_its_admin_folder(
    repo: Path, tmp_path: Path
) -> None:
    """git 2.48's `worktree.useRelativePaths` records `gitdir` relative to the admin folder;
    2.43 cannot write one, so the file is rewritten the way 2.48 would."""
    cli = GitCli()
    tree = tmp_path / "wt"
    await cli.add_worktree(repo, tree, "ra/abcd1234", _git(repo, "rev-parse", "HEAD"))
    admin = Path(_git(tree, "rev-parse", "--absolute-git-dir"))
    (admin / "gitdir").write_text(os.path.relpath(tree / ".git", admin) + "\n", encoding="utf-8")
    orphan = _git(tree, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "ref only")
    _git(tree, "update-ref", "refs/worktree/save", orphan)

    assert await cli.unmerged_history(repo, tree, "ra/abcd1234", "main") == 1


def test_a_failed_reflog_read_is_no_answer_for_a_ref_that_exists() -> None:
    """git 2.43 exits 0 for a ref with no reflog and skips an entry whose commit is gone; it
    fails only for a missing ref. A failure for one that exists is therefore never "none"."""
    from remote_agents.adapters.git.checkout import GitResult, _reflog

    failed = GitResult(128, b"", b"fatal: unreadable reflog\n")
    assert _reflog(failed, exists=False) == set()
    with pytest.raises(GitRefused):
        _reflog(failed, exists=True)


async def test_merge_base_names_where_two_branches_met_or_none(repo: Path, tmp_path: Path) -> None:
    cli = GitCli()
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "side")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "main")
    assert await cli.merge_base(repo, "main", "side") == base
    _git(repo, "checkout", "-q", "--orphan", "lonely")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "unrelated")
    assert await cli.merge_base(repo, "lonely", "main") is None


@pytest.mark.parametrize(
    ("text", "counts"),
    [
        ("", (0, 0, 0)),
        (" 1 file changed, 3 insertions(+), 1 deletion(-)\n", (1, 3, 1)),
        (" 2 files changed, 5 insertions(+)\n", (2, 5, 0)),
        (" 1 file changed, 2 deletions(-)\n", (1, 0, 2)),
        (" 1 file changed, 0 insertions(+), 0 deletions(-)\n", (1, 0, 0)),
    ],
)
def test_shortstat_reads_every_shape_git_prints(text: str, counts: tuple[int, int, int]) -> None:
    from remote_agents.adapters.git.checkout import _shortstat

    assert _shortstat(text) == counts


async def test_status_past_the_cap_ends_in_a_marker_not_a_refusal(repo: Path) -> None:
    # More ignored files than the read cap holds: a `.venv` or `node_modules`. The folder is
    # certainly not just its copies, so the listing says so instead of failing the read.
    (repo / ".gitignore").write_text("build/\n")
    build = repo / "build"
    build.mkdir()
    for index in range(1500):
        (build / f"an-ignored-file-with-a-long-name-{index:05d}.o").write_text("")

    lines = await GitCli().status_lines(repo)

    assert lines[-1].startswith("T? ")
    assert any(line.startswith("!! build/") for line in lines)
