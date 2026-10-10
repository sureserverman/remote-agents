"""`Backend.git_summary` against real repositories in `tmp_path`: read live, bounded, never
stored (BL-117, DEC-090)."""

from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.git.checkout import GitCli
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
from remote_agents.application.session_folders import SessionFolders
from remote_agents.application.session_views import git_line
from remote_agents.application.worktrees import WorktreeService
from remote_agents.composition.backend import _git_summary_reader
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

PROJECT = ProjectId("editor")


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
    for index in range(6):
        (root / f"f{index}.txt").write_text("".join(f"line {n}\n" for n in range(20)))
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _plant(tree: Path) -> None:
    """+182 / -74 over six tracked files."""
    for index in range(6):
        lines = [f"line {n}\n" for n in range(20)]
        removed = 13 if index < 4 else 11  # 4*13 + 2*11 = 74
        added = 31 if index < 2 else 30  # 2*31 + 4*30 = 182
        kept = lines[removed:]
        new = [f"new {index}.{n}\n" for n in range(added)]
        (tree / f"f{index}.txt").write_text("".join(new + kept))


class _Harness:
    def __init__(self, tmp_path: Path, repo: Path) -> None:
        self.connection = open_database(tmp_path / "sessions.sqlite3")
        self.sessions = SQLiteSessionStore(self.connection)
        self.worktrees = SQLiteWorktreeStore(self.connection)
        paths = {PROJECT: repo}
        self.service = WorktreeService(
            git=GitCli(),
            store=self.worktrees,
            sessions=self.sessions,
            project_paths=paths,
            directory=tmp_path / "state" / "worktrees",
            copy_for=lambda _path: (),
        )
        self.folders = SessionFolders(paths, self.worktrees)

    def reader(self, git=None, budget: float = 2.0):
        return _git_summary_reader(
            self.sessions, self.folders, self.worktrees, git or GitCli(), budget=budget
        )

    async def session(self) -> SessionId:
        record = SessionRecord(
            SessionId.new(),
            PROJECT,
            ProfileId("codex"),
            SessionDisplayIdentity("editor", "codex", "regular", 1),
            SessionState.RUNNING,
            datetime(2026, 10, 10, tzinfo=UTC),
        )
        await self.sessions.save(record)
        return record.session_id

    def snapshot(self) -> tuple:
        schema = tuple(self.connection.execute("SELECT sql FROM sqlite_master ORDER BY name"))
        tables = [
            r[0]
            for r in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        counts = tuple(
            self.connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in tables
        )
        changes = self.connection.total_changes
        return schema, counts, changes


async def test_a_shared_folder_reads_its_whole_checkout_against_head(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    session = await harness.session()
    _plant(repo)

    read = await harness.reader()(session)

    assert read is not None and read.shared
    assert git_line(read.summary, shared=read.shared) == "checkout: main · 6 files +182 −74 · dirty"


async def test_a_worktree_reads_its_own_changes_against_its_base(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    session = await harness.session()
    worktree = await harness.service.create(session, PROJECT)
    for name in ("one", "two"):
        (worktree.path / f"{name}.txt").write_text(f"{name}\n")
        _git(worktree.path, "add", f"{name}.txt")
        _git(worktree.path, "commit", "-q", "-m", name)
    # A change in the main checkout is not this session's.
    (repo / "f0.txt").write_text("elsewhere\n")

    read = await harness.reader()(session)

    assert read is not None and not read.shared
    assert git_line(read.summary, shared=False) == f"{worktree.branch} · 2 files +2 −0 · 2 ahead"


async def test_a_hung_git_answers_none_within_the_budget(tmp_path: Path, repo: Path) -> None:
    class _Hung(GitCli):
        """A git the kernel cannot kill yet: it ignores its cancel for a while, as a process
        in uninterruptible I/O does, so a reader that waited on its reap would wait too."""

        async def diff_summary(self, path, base, copies=None):
            try:
                await asyncio.sleep(1.2)
            except asyncio.CancelledError:
                await asyncio.sleep(1.2)
                raise

    harness = _Harness(tmp_path, repo)
    session = await harness.session()

    started = time.monotonic()
    read = await harness.reader(git=_Hung(), budget=0.2)(session)

    assert read is None
    assert time.monotonic() - started < 0.6
    await asyncio.sleep(1.4)  # let the stand-in finish before the loop closes


async def test_a_folder_outside_git_or_an_unknown_session_answers_none(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    session = await harness.session()
    _git(repo, "init", "-q")  # still a repository: sanity
    plain = tmp_path / "plain"
    plain.mkdir()
    harness.folders = SessionFolders({PROJECT: plain}, harness.worktrees)

    assert await harness.reader()(session) is None
    assert await harness.reader()(SessionId.new()) is None


async def test_reading_writes_nothing_to_any_store(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    shared = await harness.session()
    owned = await harness.session()
    await harness.service.create(owned, PROJECT)
    _plant(repo)
    before = harness.snapshot()

    for session in (shared, owned, shared):
        assert await harness.reader()(session) is not None

    assert harness.snapshot() == before


async def test_an_untouched_copy_is_not_dirt_and_an_edited_one_is(
    tmp_path: Path, repo: Path
) -> None:
    (repo / "CLAUDE.md").write_text("instructions\n")  # untracked, not ignored
    harness = _Harness(tmp_path, repo)
    harness.service._copy_for = lambda _path: ("CLAUDE.md",)
    session = await harness.session()
    worktree = await harness.service.create(session, PROJECT)
    assert "CLAUDE.md" in worktree.copied

    fresh = await harness.reader()(session)
    (worktree.path / "CLAUDE.md").write_text("edited by the agent\n")
    edited = await harness.reader()(session)

    assert fresh is not None and fresh.summary.dirty is False
    assert git_line(fresh.summary, shared=False) == f"{worktree.branch} · clean"
    assert edited is not None and edited.summary.dirty is True


async def test_a_base_merged_into_the_worktree_is_not_counted_as_its_work(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    session = await harness.session()
    worktree = await harness.service.create(session, PROJECT)
    (worktree.path / "own.txt").write_text("own\n")
    _git(worktree.path, "add", "own.txt")
    _git(worktree.path, "commit", "-q", "-m", "own")
    for index in range(3):
        (repo / f"main{index}.txt").write_text("".join(f"{n}\n" for n in range(50)))
        _git(repo, "add", f"main{index}.txt")
        _git(repo, "commit", "-q", "-m", f"main {index}")
    _git(worktree.path, "rebase", "-q", "main")

    read = await harness.reader()(session)

    assert read is not None
    assert git_line(read.summary, shared=False) == f"{worktree.branch} · 1 file +1 −0 · 1 ahead"


async def test_a_change_too_large_for_a_numstat_still_has_a_line(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    session = await harness.session()
    folder = repo / ("a-deliberately-long-directory-name-" * 2)
    folder.mkdir()
    names = [folder / f"file-with-a-long-name-{index:05d}.txt" for index in range(1200)]
    for name in names:
        name.write_text("one\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "many")
    for name in names:
        name.write_text("two\n")

    read = await harness.reader()(session)

    assert read is not None
    assert (read.summary.files, read.summary.added, read.summary.removed) == (1200, 1200, 1200)


async def test_sessions_sharing_a_checkout_share_one_measurement(
    tmp_path: Path, repo: Path
) -> None:
    class _Counting(GitCli):
        calls = 0

        async def diff_summary(self, path, base, copies=None):
            _Counting.calls += 1
            await asyncio.sleep(0.05)
            return await super().diff_summary(path, base, copies)

    harness = _Harness(tmp_path, repo)
    sessions = [await harness.session() for _ in range(4)]
    read = harness.reader(git=_Counting())

    reads = await asyncio.gather(*(read(session) for session in sessions))

    assert all(r is not None and r.shared for r in reads)
    assert _Counting.calls == 1
    await read(sessions[0])
    assert _Counting.calls == 2, "a later read measures again; only concurrent ones share"
