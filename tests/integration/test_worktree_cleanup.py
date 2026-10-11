"""The worktree cleanup pass against real repositories in `tmp_path` (BL-116).

A folder goes, with its branch, only when its branch is merged into its base, it holds nothing
but unchanged copies, and no live session or open rollover uses it. One case per guard, each
keeping the folder; then the one case where everything holds and the folder, the branch and
the row's liveness all go -- while the base branch keeps the commits.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.git.checkout import GitCli
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
from remote_agents.application.worktree_cleanup import (
    DETACHED_BASE,
    DIRTY,
    IN_USE,
    MOVED,
    NOT_A_REPOSITORY,
    UNMERGED,
    CleanupReport,
    WorktreeCleanupPass,
)
from remote_agents.application.worktrees import WorktreeService
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.rollover import RolloverState
from remote_agents.ports.git_checkout import GitRefused
from remote_agents.ports.worktree_store import Worktree

PROJECT = ProjectId("editor")
CODEX = ProfileId("codex")
AT = datetime(2026, 10, 10, tzinfo=UTC)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _host_git_config_is_not_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
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
    (root / ".gitignore").write_text(".claude/\nCLAUDE.md\nbuild/\n", encoding="utf-8")
    (root / "f.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "f.txt")
    _git(root, "commit", "-q", "-m", "base")
    (root / "CLAUDE.md").write_text("instructions\n", encoding="utf-8")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
    (root / ".claude" / "my notes").mkdir()
    (root / ".claude" / "my notes" / "local file.md").write_text("notes\n", encoding="utf-8")
    return root


class _Git(GitCli):
    """The real git boundary, with one verb made to refuse or fail on demand."""

    def __init__(self) -> None:
        super().__init__()
        self.fail: dict[str, BaseException] = {}

    async def _maybe(self, verb: str) -> None:
        if verb in self.fail:
            raise self.fail[verb]

    async def remove_worktree(self, repo: Path, path: Path) -> None:
        await self._maybe("remove_worktree")
        await super().remove_worktree(repo, path)

    async def delete_merged_branch(self, repo: Path, branch: str) -> None:
        await self._maybe("delete_merged_branch")
        await super().delete_merged_branch(repo, branch)

    async def status_lines(self, path: Path) -> tuple[str, ...]:
        await self._maybe("status_lines")
        return await super().status_lines(path)

    async def is_ancestor(self, repo: Path, commit: str, ref: str) -> bool:
        await self._maybe("is_ancestor")
        return await super().is_ancestor(repo, commit, ref)


class _Harness:
    def __init__(self, tmp_path: Path, project: Path) -> None:
        connection = open_database(tmp_path / "sessions.sqlite3")
        self.connection = connection
        self.sessions = SQLiteSessionStore(connection)
        self.store = SQLiteWorktreeStore(connection)
        self.rollovers = SQLiteRolloverStore(connection)
        self.repo = project
        self.worktrees = WorktreeService(
            git=GitCli(),
            store=self.store,
            sessions=self.sessions,
            project_paths={PROJECT: project},
            directory=tmp_path / "state" / "worktrees",
            copy_for=lambda _path: (
                "CLAUDE.md",
                ".claude/settings.local.json",
                ".claude/my notes/local file.md",
            ),
        )
        self.git = _Git()
        self.cleanup = WorktreeCleanupPass(
            git=self.git,
            store=self.store,
            sessions=self.sessions,
            rollovers=self.rollovers,
            repository=self.worktrees.repository,
        )

    async def session(self, state: SessionState = SessionState.RUNNING) -> SessionId:
        record = SessionRecord(
            SessionId.new(),
            PROJECT,
            CODEX,
            SessionDisplayIdentity("editor", "codex", "regular", 1),
            state,
            AT,
        )
        await self.sessions.save(record)
        return record.session_id

    async def worktree(self) -> tuple[SessionId, Worktree]:
        session = await self.session()
        return session, await self.worktrees.create(session, PROJECT)

    def end(self, session: SessionId) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE sessions SET state = 'ended' WHERE session_id = ?", (str(session),)
            )

    async def row(self, path: Path) -> Worktree:
        (found,) = [w for w in await self._all() if w.path == path]
        return found

    async def _all(self) -> tuple[Worktree, ...]:
        rows = self.connection.execute("SELECT session_id FROM session_worktrees").fetchall()
        found = {}
        for (session_id,) in rows:
            worktree = await self.store.folder_for(SessionId(session_id))
            assert worktree is not None
            found[worktree.path] = worktree
        return tuple(found.values())


def _branches(repo: Path) -> set[str]:
    return set(_git(repo, "branch", "--format=%(refname:short)").splitlines())


def _commit_in(tree: Path, name: str = "work.txt") -> str:
    (tree / name).write_text("work\n", encoding="utf-8")
    _git(tree, "add", name)
    _git(tree, "commit", "-q", "-m", f"add {name}")
    return _git(tree, "rev-parse", "HEAD")


async def _kept(harness: _Harness, worktree: Worktree) -> None:
    assert worktree.path.is_dir()
    assert worktree.branch in _branches(harness.repo)
    assert (await harness.row(worktree.path)).removed_at is None


async def _bindable(harness: _Harness, worktree: Worktree) -> None:
    """The claim was given back: a resume can bind a session to the folder right away."""
    await harness.store.bind(await harness.session(), worktree.path)


# -- what removes ------------------------------------------------------------------------------


async def test_a_merged_clean_unreferenced_worktree_and_its_branch_are_removed(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    work = _commit_in(worktree.path)
    _git(repo, "merge", "-q", "--ff-only", worktree.branch)
    harness.end(session)

    report = await harness.cleanup.pass_once()

    assert report.removed == (worktree.path,)
    assert not worktree.path.exists()
    assert worktree.branch not in _branches(repo)
    assert _git(repo, "merge-base", "--is-ancestor", work, "main") == ""
    assert (await harness.row(worktree.path)).removed_at is not None
    assert worktree.path not in {w.path for w in await harness.store.live_for_project(PROJECT)}
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


async def test_a_folder_already_gone_with_a_stale_git_entry_is_cleared_with_its_branch(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    shutil.rmtree(worktree.path)
    assert "prunable" in _git(repo, "worktree", "list", "--porcelain")
    harness.end(session)

    report = await harness.cleanup.pass_once()

    assert report.removed == (worktree.path,)
    assert worktree.branch not in _branches(repo)
    assert "prunable" not in _git(repo, "worktree", "list", "--porcelain")
    assert (await harness.row(worktree.path)).removed_at is not None


async def test_a_gone_folder_whose_branch_is_unmerged_is_kept(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    _commit_in(worktree.path)
    shutil.rmtree(worktree.path)
    harness.end(session)

    report = await harness.cleanup.pass_once()

    assert report.kept == {worktree.path: UNMERGED}
    assert worktree.branch in _branches(repo)
    assert (await harness.row(worktree.path)).removed_at is None


# -- what keeps: one case per guard ------------------------------------------------------------


async def test_an_unmerged_branch_is_kept(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    _commit_in(worktree.path)
    harness.end(session)

    report = await harness.cleanup.pass_once()

    assert report.kept == {worktree.path: UNMERGED}
    await _kept(harness, worktree)


async def test_a_branch_merged_only_into_another_branch_is_kept(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    _commit_in(worktree.path)
    _git(repo, "branch", "other", worktree.branch)
    harness.end(session)

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: UNMERGED}
    await _kept(harness, worktree)


async def test_an_edited_copied_file_is_kept(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    (worktree.path / "CLAUDE.md").write_text("edited in the worktree\n", encoding="utf-8")
    harness.end(session)

    report = await harness.cleanup.pass_once()

    assert report.kept == {worktree.path: DIRTY}
    await _kept(harness, worktree)


async def test_a_copied_file_replaced_by_a_symlink_is_kept(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("instructions\n", encoding="utf-8")
    (worktree.path / "CLAUDE.md").unlink()
    (worktree.path / "CLAUDE.md").symlink_to(elsewhere)
    harness.end(session)

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: DIRTY}
    await _kept(harness, worktree)


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("notes.txt", id="untracked"),
        pytest.param("build/out.bin", id="ignored"),
        pytest.param(".claude/other.json", id="ignored-beside-a-copy"),
    ],
)
async def test_a_new_file_is_kept(tmp_path: Path, repo: Path, relative: str) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    target = worktree.path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("new\n", encoding="utf-8")
    harness.end(session)

    report = await harness.cleanup.pass_once()

    assert report.kept == {worktree.path: DIRTY}
    await _kept(harness, worktree)
    assert target.exists()


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
async def test_a_tracked_file_hidden_from_status_is_kept(
    tmp_path: Path, repo: Path, flag: str
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    _git(worktree.path, "update-index", flag, "f.txt")
    (worktree.path / "f.txt").write_text("edited where status cannot see\n", encoding="utf-8")
    harness.end(session)

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: DIRTY}
    await _kept(harness, worktree)


async def test_a_live_successor_bound_to_the_folder_keeps_it(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    predecessor, worktree = await harness.worktree()
    successor = await harness.session()
    await harness.store.bind(successor, worktree.path)
    harness.end(predecessor)

    report = await harness.cleanup.pass_once()

    assert worktree.path not in report.removed
    assert await harness.cleanup.verdict(worktree) == IN_USE
    await _kept(harness, worktree)


async def test_an_open_rollover_of_a_bound_session_keeps_the_folder(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    predecessor, worktree = await harness.worktree()
    assert await harness.rollovers.request(predecessor, project_id=PROJECT, profile_id=CODEX, at=AT)
    harness.end(predecessor)

    report = await harness.cleanup.pass_once()

    assert report.kept == {worktree.path: IN_USE}
    await _kept(harness, worktree)
    await _bindable(harness, worktree)


async def test_a_worktree_cut_from_a_detached_head_is_kept(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    _git(repo, "checkout", "-q", "--detach")
    session, worktree = await harness.worktree()
    assert worktree.base_branch is None
    harness.end(session)

    report = await harness.cleanup.pass_once()

    assert worktree.path not in report.removed
    assert await harness.cleanup.verdict(worktree) == DETACHED_BASE
    await _kept(harness, worktree)


# -- how it keeps -------------------------------------------------------------------------------


async def test_a_kept_folder_is_logged_once_per_change_of_reason(
    tmp_path: Path, repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    _commit_in(worktree.path)
    harness.end(session)
    caplog.set_level(logging.INFO, logger="remote_agents.application.worktree_cleanup")

    for _ in range(3):
        await harness.cleanup.pass_once()
    (worktree.path / "notes.txt").write_text("new\n", encoding="utf-8")
    _git(repo, "merge", "-q", "--ff-only", worktree.branch)
    await harness.cleanup.pass_once()

    kept = [r.getMessage() for r in caplog.records if " kept: " in r.getMessage()]
    assert kept == [
        f"worktree {worktree.path} kept: {UNMERGED}",
        f"worktree {worktree.path} kept: {DIRTY}",
    ]


async def test_a_claim_left_by_a_dead_pass_is_released_and_the_folder_removed(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    harness.end(session)
    assert await harness.store.claim_for_removal(worktree.path, at=AT)

    assert (await harness.cleanup.pass_once()).removed == (worktree.path,)


async def test_commits_made_on_a_detached_head_in_the_worktree_keep_it(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    _git(worktree.path, "checkout", "-q", "--detach")
    lost = _commit_in(worktree.path)
    harness.end(session)

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: MOVED}
    await _kept(harness, worktree)
    assert _git(worktree.path, "rev-parse", "HEAD") == lost


async def test_a_gone_folder_whose_entry_holds_detached_commits_is_kept(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    _git(worktree.path, "checkout", "-q", "--detach")
    lost = _commit_in(worktree.path)
    shutil.rmtree(worktree.path)
    harness.end(session)

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: MOVED}
    assert lost in _git(repo, "worktree", "list", "--porcelain")


async def test_a_worktree_switched_to_another_branch_is_kept(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    _git(worktree.path, "checkout", "-q", "-b", "elsewhere")
    _commit_in(worktree.path)
    harness.end(session)

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: MOVED}
    await _kept(harness, worktree)


async def test_an_open_rollover_whose_successor_is_bound_keeps_the_folder(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    first, worktree = await harness.worktree()
    harness.end(first)
    # A predecessor outside the folder whose rollover's successor was bound into it.
    elsewhere = await harness.session()
    rollover = await harness.rollovers.request(
        elsewhere, project_id=PROJECT, profile_id=CODEX, at=AT
    )
    assert rollover is not None
    rollover = await harness.rollovers.advance(
        rollover.id, RolloverState.HANDOFF_READY, at=AT, detail="ready"
    )
    rollover = await harness.rollovers.advance(rollover.id, RolloverState.SUCCESSOR_STARTING, at=AT)
    successor = await harness.session()
    await harness.store.bind(successor, worktree.path)
    await harness.rollovers.record_successor(rollover.id, successor, at=AT)
    harness.end(successor)

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: IN_USE}
    await _kept(harness, worktree)


async def test_a_session_bound_between_the_candidate_read_and_the_claim_wins(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    harness.end(session)
    store = harness.store
    real_claim = store.claim_for_removal

    async def claim_after_a_resume(path: Path, *, at: datetime) -> bool:
        await store.bind(await harness.session(), path)
        return await real_claim(path, at=at)

    store.claim_for_removal = claim_after_a_resume  # type: ignore[method-assign]

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: IN_USE}
    await _kept(harness, worktree)


async def test_a_branch_git_refuses_to_delete_after_the_folder_went_is_reported(
    tmp_path: Path, repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    harness.end(session)
    harness.git.fail["delete_merged_branch"] = GitRefused("error: branch is not fully merged")
    caplog.set_level(logging.WARNING, logger="remote_agents.application.worktree_cleanup")

    assert (await harness.cleanup.pass_once()).removed == (worktree.path,)

    assert not worktree.path.exists()
    assert worktree.branch in _branches(repo)
    assert (await harness.row(worktree.path)).removed_at is not None
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert f"branch -d {worktree.branch}" in warning


@pytest.mark.parametrize(
    ("verb", "error", "reason"),
    [
        pytest.param(
            "remove_worktree",
            GitRefused("fatal: cannot remove a locked working tree"),
            "git: fatal: cannot remove a locked working tree",
            id="remove-refused",
        ),
        pytest.param(
            "is_ancestor", RuntimeError("store hiccup"), "fault: RuntimeError", id="fault"
        ),
    ],
)
async def test_a_refusal_or_fault_keeps_the_folder_and_gives_the_claim_back(
    tmp_path: Path, repo: Path, verb: str, error: BaseException, reason: str
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    harness.end(session)
    harness.git.fail[verb] = error

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: reason}
    await _kept(harness, worktree)
    await _bindable(harness, worktree)


async def test_a_copied_folder_replaced_by_a_symlink_is_kept(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    elsewhere = tmp_path / "elsewhere"
    shutil.copytree(worktree.path / ".claude", elsewhere)
    shutil.rmtree(worktree.path / ".claude")
    (worktree.path / ".claude").symlink_to(elsewhere)
    harness.end(session)

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: DIRTY}
    await _kept(harness, worktree)


async def test_a_folder_another_pass_holds_is_left_to_that_pass(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    harness.end(session)
    store = harness.store
    real_claim = store.claim_for_removal

    async def claimed_by_another_pass_first(path: Path, *, at: datetime) -> bool:
        assert await real_claim(path, at=at)
        return await real_claim(path, at=at)

    store.claim_for_removal = claimed_by_another_pass_first  # type: ignore[method-assign]

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: IN_USE}
    await _kept(harness, worktree)


async def test_a_folder_git_holds_no_entry_for_is_kept(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    shutil.rmtree(repo / ".git" / "worktrees" / worktree.path.name)
    harness.end(session)

    report = await harness.cleanup.pass_once()

    assert report.kept == {worktree.path: "git: no worktree entry for the folder"}
    await _kept(harness, worktree)


async def test_a_folder_whose_entry_and_branch_are_already_gone_is_marked_removed(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    shutil.rmtree(worktree.path)
    _git(repo, "worktree", "prune")
    _git(repo, "branch", "-d", worktree.branch)
    harness.end(session)

    assert (await harness.cleanup.pass_once()).removed == (worktree.path,)
    assert (await harness.row(worktree.path)).removed_at is not None


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(GitRefused("error: branch is not fully merged"), id="refused"),
        pytest.param(OSError("disk went away"), id="fault"),
    ],
)
async def test_a_gone_folder_whose_branch_git_will_not_delete_is_still_marked_removed(
    tmp_path: Path, repo: Path, caplog: pytest.LogCaptureFixture, error: BaseException
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    shutil.rmtree(worktree.path)
    harness.end(session)
    harness.git.fail["delete_merged_branch"] = error
    caplog.set_level(logging.WARNING, logger="remote_agents.application.worktree_cleanup")

    assert (await harness.cleanup.pass_once()).removed == (worktree.path,)

    assert worktree.branch in _branches(repo)
    assert "prunable" not in _git(repo, "worktree", "list", "--porcelain")
    assert (await harness.row(worktree.path)).removed_at is not None
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert f"branch -d {worktree.branch}" in warning


@pytest.mark.parametrize("verb", ["is_ancestor", "remove_worktree"])
async def test_a_cancel_mid_removal_gives_the_claim_back(
    tmp_path: Path, repo: Path, verb: str
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    harness.end(session)
    harness.git.fail[verb] = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await harness.cleanup.pass_once()

    await _kept(harness, worktree)
    await _bindable(harness, worktree)


async def test_doctor_s_listing_names_each_kept_worktree_with_its_reason(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    unmerged_session, unmerged = await harness.worktree()
    _commit_in(unmerged.path)
    dirty_session, dirty = await harness.worktree()
    (dirty.path / "notes.txt").write_text("new\n", encoding="utf-8")
    _live, in_use = await harness.worktree()
    gone_session, gone = await harness.worktree()
    harness.end(unmerged_session)
    harness.end(dirty_session)
    harness.end(gone_session)
    await harness.cleanup.pass_once()

    assert await harness.cleanup.kept() == (
        (str(unmerged.path), unmerged.branch, UNMERGED),
        (str(dirty.path), dirty.branch, DIRTY),
        (str(in_use.path), in_use.branch, IN_USE),
    )
    assert not gone.path.exists()


async def test_doctor_reads_the_kept_worktrees_from_a_store_at_this_build_s_schema(
    tmp_path: Path, repo: Path
) -> None:
    from types import SimpleNamespace

    from remote_agents.bootstrap import _kept_worktrees

    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    _commit_in(worktree.path)
    harness.end(session)
    config = SimpleNamespace(
        database_path=tmp_path / "sessions.sqlite3",
        registry_path=tmp_path / "registry.toml",
        dev_root=tmp_path / "dev",
    )

    # `doctor` runs outside any event loop; so does the listing it makes.
    rows = await asyncio.to_thread(_kept_worktrees, config)

    # No catalogue names the project here, so git cannot be asked: kept, and said why.
    assert rows == ((str(worktree.path), worktree.branch, NOT_A_REPOSITORY),)
    config.database_path = tmp_path / "missing.sqlite3"
    assert await asyncio.to_thread(_kept_worktrees, config) is None


async def test_a_binding_whose_session_row_is_missing_keeps_the_folder(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    first, worktree = await harness.worktree()
    harness.end(first)
    vanished = await harness.session(SessionState.ENDED)
    await harness.store.bind(vanished, worktree.path)
    # A row lost to a hand-edited or restored database; the binding outlived it.
    harness.connection.execute("PRAGMA foreign_keys = OFF")
    with harness.connection:
        harness.connection.execute("DELETE FROM sessions WHERE session_id = ?", (str(vanished),))
    harness.connection.execute("PRAGMA foreign_keys = ON")

    assert await harness.cleanup.verdict(worktree) == IN_USE
    assert await harness.cleanup.kept() == ((str(worktree.path), worktree.branch, IN_USE),)
    await harness.cleanup.pass_once()
    await _kept(harness, worktree)


async def test_a_base_branch_deleted_since_keeps_the_folder(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    _git(repo, "checkout", "-q", "-b", "feature")
    session, worktree = await harness.worktree()
    _git(repo, "checkout", "-q", "main")
    _git(repo, "branch", "-q", "-D", "feature")
    harness.end(session)

    (reason,) = (await harness.cleanup.pass_once()).kept.values()
    assert reason.startswith("git: ")
    await _kept(harness, worktree)


async def test_a_staged_change_is_kept(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    (worktree.path / "f.txt").write_text("staged, not committed\n", encoding="utf-8")
    _git(worktree.path, "add", "f.txt")
    harness.end(session)

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: DIRTY}
    await _kept(harness, worktree)


async def test_a_folder_that_left_the_candidates_is_logged_again_when_it_returns(
    tmp_path: Path, repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    _commit_in(worktree.path)
    harness.end(session)
    caplog.set_level(logging.INFO, logger="remote_agents.application.worktree_cleanup")

    await harness.cleanup.pass_once()
    resumed = await harness.session()
    await harness.store.bind(resumed, worktree.path)
    await harness.cleanup.pass_once()
    harness.end(resumed)
    await harness.cleanup.pass_once()

    kept = [r.getMessage() for r in caplog.records if " kept: " in r.getMessage()]
    assert kept == [f"worktree {worktree.path} kept: {UNMERGED}"] * 2


@pytest.mark.parametrize("how", ["reset-away", "detached-then-back", "worktree-ref"])
async def test_a_commit_only_the_worktree_s_refs_or_reflogs_reach_keeps_it(
    tmp_path: Path, repo: Path, how: str
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    if how == "reset-away":
        _commit_in(worktree.path)
        _git(worktree.path, "reset", "-q", "--hard", "HEAD~1")
    elif how == "detached-then-back":
        _git(worktree.path, "checkout", "-q", "--detach")
        _commit_in(worktree.path)
        _git(worktree.path, "checkout", "-q", worktree.branch)
    else:
        _git(worktree.path, "checkout", "-q", "--detach")
        held = _commit_in(worktree.path)
        _git(worktree.path, "update-ref", "refs/worktree/save", held)
        _git(worktree.path, "checkout", "-q", worktree.branch)
    harness.end(session)

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: UNMERGED}
    await _kept(harness, worktree)


async def test_an_entry_named_git_below_the_top_keeps_the_folder(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    (worktree.path / "build").mkdir()
    (worktree.path / "build" / ".git").write_text("work git never lists\n", encoding="utf-8")
    harness.end(session)

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: DIRTY}
    await _kept(harness, worktree)


async def test_a_folder_replaced_by_a_symlink_is_kept(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    moved = tmp_path / "moved"
    worktree.path.rename(moved)
    worktree.path.symlink_to(moved)
    harness.end(session)

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: DIRTY}
    assert moved.is_dir() and worktree.path.is_symlink()


async def test_a_locked_entry_whose_folder_is_away_keeps_its_row(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    _git(repo, "worktree", "lock", "--reason", "usb", str(worktree.path))
    away = tmp_path / "unmounted"
    worktree.path.rename(away)
    harness.end(session)

    (reason,) = (await harness.cleanup.pass_once()).kept.values()

    assert "locked" in reason
    assert worktree.branch in _branches(repo)
    assert (await harness.row(worktree.path)).removed_at is None
    assert away.is_dir()
    await _bindable(harness, worktree)


async def test_only_the_first_pass_gives_back_claims_a_dead_run_left(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    harness.end(session)
    _commit_in(worktree.path)
    await harness.cleanup.pass_once()
    # A claim taken after the first pass is a live one, never this pass's to give back.
    _git(repo, "merge", "-q", "--ff-only", worktree.branch)
    assert await harness.store.claim_for_removal(worktree.path, at=AT)

    assert await harness.cleanup.pass_once() == CleanupReport(removed=(), kept={})
    await _kept(harness, worktree)


async def test_a_kept_folder_costs_the_store_no_write_on_a_pass(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    _commit_in(worktree.path)
    harness.end(session)
    await harness.cleanup.pass_once()  # the first pass gives back stale claims: a write
    before = harness.connection.total_changes

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: UNMERGED}
    assert harness.connection.total_changes == before


async def test_a_folder_with_more_ignored_files_than_status_can_list_is_dirty(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    session, worktree = await harness.worktree()
    harness.end(session)
    (worktree.path / ".gitignore").write_text("build/\n")
    build = worktree.path / "build"
    build.mkdir()
    for index in range(1500):
        (build / f"an-ignored-file-with-a-long-name-{index:05d}.o").write_text("")

    assert (await harness.cleanup.pass_once()).kept == {worktree.path: DIRTY}
    await _kept(harness, worktree)
