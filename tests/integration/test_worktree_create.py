"""Creating a session worktree against a real repository in `tmp_path`, all or nothing (BL-116).

The offer is made only on contention in a git project; creation branches at the main
checkout's HEAD, copies the untracked files the list names, and records the folder bound to
its session. Every failure part-way leaves no folder, no branch and no row behind -- and never
deletes a branch it did not make.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.git.checkout import GitCli
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.services import SessionService
from remote_agents.application.worktrees import (
    NEVER_COPIED,
    WorktreeCreateFailed,
    WorktreeService,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.git_checkout import GitRefused
from remote_agents.ports.terminal import INVALID_INTENT, TerminalObservation
from remote_agents.ports.worktree_store import Worktree

PROJECT = ProjectId("editor")
CODEX = ProfileId("codex")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _host_git_config_is_not_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    (root / ".gitignore").write_text(".claude/\nCLAUDE.md\n.env\n", encoding="utf-8")
    (root / "f.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "f.txt")
    _git(root, "commit", "-q", "-m", "base")
    return root


class _Harness:
    def __init__(
        self,
        tmp_path: Path,
        project: Path,
        *,
        copy: Sequence[str] = ("CLAUDE.md", "AGENTS.md", ".claude/settings.local.json"),
        git: GitCli | None = None,
    ) -> None:
        connection = open_database(tmp_path / "sessions.sqlite3")
        self.sessions = SQLiteSessionStore(connection)
        self.store = SQLiteWorktreeStore(connection)
        self.connection = connection
        self.directory = tmp_path / "state" / "worktrees"
        self.directory.mkdir(parents=True)
        self.project = project
        self.service = WorktreeService(
            git=git or GitCli(),
            store=self.store,
            sessions=self.sessions,
            project_paths={PROJECT: project},
            directory=self.directory,
            copy_for=lambda _path: tuple(copy),
        )

    async def session(self, state: SessionState = SessionState.STARTING) -> SessionId:
        record = SessionRecord(
            SessionId.new(),
            PROJECT,
            CODEX,
            SessionDisplayIdentity("editor", "codex", "regular", 1),
            state,
            datetime(2026, 10, 10, tzinfo=UTC),
        )
        await self.sessions.save(record)
        return record.session_id

    def rows(self) -> tuple[int, int]:
        return (
            self.connection.execute("SELECT COUNT(*) FROM worktrees").fetchone()[0],
            self.connection.execute("SELECT COUNT(*) FROM session_worktrees").fetchone()[0],
        )


def _branches(repo: Path) -> set[str]:
    return set(_git(repo, "branch", "--format=%(refname:short)").splitlines())


# -- the offer ---------------------------------------------------------------------------------


async def test_the_offer_is_made_only_on_contention_in_a_git_project(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    assert not await harness.service.offered(PROJECT)

    for idle in (
        SessionState.ENDED,
        SessionState.FAILED,
        SessionState.PRESERVED,
        SessionState.ORPHANED,
    ):
        await harness.session(idle)
    assert not await harness.service.offered(PROJECT), "no agent is at work in the folder"

    await harness.session(SessionState.RUNNING)
    assert await harness.service.offered(PROJECT)


@pytest.mark.parametrize(
    "state", [SessionState.STARTING, SessionState.UNTRUSTED, SessionState.STOP_REQUESTED]
)
async def test_an_agent_still_at_work_is_contention(
    tmp_path: Path, repo: Path, state: SessionState
) -> None:
    harness = _Harness(tmp_path, repo)
    await harness.session(state)
    assert await harness.service.offered(PROJECT)


async def test_a_project_outside_git_is_never_offered(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    harness = _Harness(tmp_path, plain)
    await harness.session(SessionState.RUNNING)
    assert not await harness.service.offered(PROJECT)


async def test_a_project_below_its_repository_top_level_is_not_offered(
    tmp_path: Path, repo: Path
) -> None:
    """A worktree is a whole checkout; a session's folder is its top level, so a project that
    is a subfolder of its repository would start one level too high. Not offered at all."""
    sub = repo / "sub"
    sub.mkdir()
    harness = _Harness(tmp_path, sub)
    session = await harness.session(SessionState.RUNNING)
    assert not await harness.service.offered(PROJECT)
    with pytest.raises(WorktreeCreateFailed):
        await harness.service.create(session, PROJECT)
    assert _branches(repo) == {"main"}


# -- creation ----------------------------------------------------------------------------------


async def test_create_branches_at_head_copies_untracked_files_and_binds_the_session(
    tmp_path: Path, repo: Path
) -> None:
    (repo / "CLAUDE.md").write_text("instructions\n", encoding="utf-8")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "AGENTS.md")
    _git(repo, "commit", "-q", "-m", "agents")
    (repo / "AGENTS.md").write_text("tracked, edited in main\n", encoding="utf-8")
    harness = _Harness(tmp_path, repo)
    session = await harness.session()

    worktree = await harness.service.create(session, PROJECT)

    assert worktree.path == harness.directory / str(PROJECT) / str(session)
    assert worktree.branch == f"ra/{str(session)[:8]}"
    assert worktree.base_branch == "main"
    assert worktree.base_commit == _git(repo, "rev-parse", "HEAD")
    assert _git(worktree.path, "rev-parse", "--abbrev-ref", "HEAD") == worktree.branch
    assert (worktree.path / "CLAUDE.md").read_text(encoding="utf-8") == "instructions\n"
    assert (worktree.path / ".claude" / "settings.local.json").is_file()
    # Tracked: the worktree's own committed copy, not the main checkout's edit.
    assert (worktree.path / "AGENTS.md").read_text(encoding="utf-8") == "tracked\n"
    assert set(worktree.copied) == {"CLAUDE.md", ".claude/settings.local.json"}
    assert await harness.store.folder_for(session) == worktree
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == "tracked, edited in main\n"


async def test_a_detached_main_checkout_records_no_base_branch(tmp_path: Path, repo: Path) -> None:
    _git(repo, "checkout", "-q", "--detach")
    harness = _Harness(tmp_path, repo)
    worktree = await harness.service.create(await harness.session(), PROJECT)
    assert worktree.base_branch is None


async def test_env_is_never_copied_even_when_a_hand_built_list_names_it(
    tmp_path: Path, repo: Path
) -> None:
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("x\n", encoding="utf-8")
    harness = _Harness(tmp_path, repo, copy=(".env", "CLAUDE.md", "sub/.ENV.local"))
    worktree = await harness.service.create(await harness.session(), PROJECT)
    assert not (worktree.path / ".env").exists()
    assert set(worktree.copied) == {"CLAUDE.md"}


async def test_a_directory_by_a_listed_name_is_skipped_and_leaks_no_descriptor(
    tmp_path: Path, repo: Path
) -> None:
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
    harness = _Harness(tmp_path, repo, copy=(".claude", ".claude/"))
    before = len(os.listdir("/proc/self/fd"))

    worktree = await harness.service.create(await harness.session(), PROJECT)

    assert worktree.copied == {}
    assert len(os.listdir("/proc/self/fd")) <= before


async def test_copied_paths_are_keyed_as_git_names_them(tmp_path: Path, repo: Path) -> None:
    """Stage 4 compares these keys with `git status` paths; `./CLAUDE.md` must not survive."""
    (repo / "CLAUDE.md").write_text("x\n", encoding="utf-8")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
    harness = _Harness(tmp_path, repo, copy=("./CLAUDE.md", ".claude//settings.local.json"))

    worktree = await harness.service.create(await harness.session(), PROJECT)

    assert set(worktree.copied) == {"CLAUDE.md", ".claude/settings.local.json"}


def test_the_copier_refuses_what_the_config_loader_refuses() -> None:
    """Two copies of one rule, because `config` and `application` may not import each other;
    this pins them together."""
    from remote_agents import config

    assert NEVER_COPIED.pattern == config._NEVER_COPIED.pattern
    assert NEVER_COPIED.flags == config._NEVER_COPIED.flags


async def test_a_symlinked_source_is_never_copied(tmp_path: Path, repo: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "CLAUDE.md").write_text("outside\n", encoding="utf-8")
    (elsewhere / "settings.local.json").write_text("{}\n", encoding="utf-8")
    os.symlink(elsewhere / "CLAUDE.md", repo / "CLAUDE.md")
    os.symlink(elsewhere, repo / ".claude")
    harness = _Harness(tmp_path, repo)

    worktree = await harness.service.create(await harness.session(), PROJECT)

    assert not os.path.lexists(worktree.path / "CLAUDE.md")
    assert not os.path.lexists(worktree.path / ".claude")
    assert worktree.copied == {}


async def test_a_symlinked_destination_is_never_written_through(tmp_path: Path, repo: Path) -> None:
    """A tracked `.claude` symlink checks out as a symlink in the worktree too; copying under
    it would write outside the checkout."""
    target = tmp_path / "target"
    target.mkdir()
    (repo / ".gitignore").write_text("CLAUDE.md\n.env\n", encoding="utf-8")
    os.symlink(target, repo / ".claude")
    _git(repo, "add", ".gitignore", ".claude")
    _git(repo, "commit", "-q", "-m", "tracked link")
    # The main checkout holds a real folder where the link is tracked, as a hand edit would.
    (repo / ".claude").unlink()
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
    _git(repo, "update-index", "--assume-unchanged", ".claude")
    harness = _Harness(tmp_path, repo)

    worktree = await harness.service.create(await harness.session(), PROJECT)

    assert list(target.iterdir()) == []
    assert ".claude/settings.local.json" not in worktree.copied


async def test_a_fifo_or_an_oversized_file_by_a_listed_name_is_skipped(
    tmp_path: Path, repo: Path
) -> None:
    """A FIFO would block the open -- and the event loop with it -- if read like a file."""
    os.mkfifo(repo / "CLAUDE.md")
    (repo / "AGENTS.md").write_bytes(b"x" * (1024 * 1024 + 1))
    harness = _Harness(tmp_path, repo)

    worktree = await asyncio.wait_for(
        harness.service.create(await harness.session(), PROJECT), timeout=10
    )

    assert worktree.copied == {}
    assert not os.path.lexists(worktree.path / "CLAUDE.md")
    assert not os.path.lexists(worktree.path / "AGENTS.md")


# -- all or nothing ----------------------------------------------------------------------------


class _AddThenFail(GitCli):
    """`worktree add` that lands -- folder and branch made -- and then reports a failure."""

    async def add_worktree(self, repo: Path, path: Path, branch: str, commit: str) -> None:
        await super().add_worktree(repo, path, branch, commit)
        raise GitRefused("fatal: simulated failure after the branch exists")


async def test_a_failed_add_leaves_no_folder_no_branch_and_no_row(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo, git=_AddThenFail())
    session = await harness.session()

    with pytest.raises(WorktreeCreateFailed, match="simulated failure"):
        await harness.service.create(session, PROJECT)

    assert _branches(repo) == {"main"}
    assert not (harness.directory / str(PROJECT)).exists(), "the per-project folder too"
    assert harness.rows() == (0, 0)
    assert "worktrees" not in os.listdir(repo / ".git") or not os.listdir(
        repo / ".git" / "worktrees"
    )


class _AddThenRefuseRemoval(_AddThenFail):
    """As a `worktree add` killed at its timeout leaves it: a folder git will not remove."""

    async def remove_worktree(self, repo: Path, path: Path) -> None:
        raise GitRefused("fatal: cannot remove a locked working tree")


async def test_a_folder_the_undo_cannot_remove_is_named_with_how_to_remove_it(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo, git=_AddThenRefuseRemoval())
    session = await harness.session()
    path = harness.directory / str(PROJECT) / str(session)

    with pytest.raises(WorktreeCreateFailed) as raised:
        await harness.service.create(session, PROJECT)

    message = str(raised.value)
    assert f"left behind: folder {path}" in message
    assert f"git -C {repo.resolve()} worktree remove --force --force {path}" in message
    assert harness.rows() == (0, 0)


async def test_a_failed_record_undoes_the_copies_the_folder_and_the_branch(
    tmp_path: Path, repo: Path
) -> None:
    (repo / "CLAUDE.md").write_text("instructions\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("untracked and not ignored\n", encoding="utf-8")
    harness = _Harness(tmp_path, repo)
    session = await harness.session()

    async def refuse(_worktree: Worktree, *, bound_to: SessionId) -> None:
        raise ValueError("worktree not recorded: simulated")

    harness.store.record = refuse  # type: ignore[method-assign]
    with pytest.raises(WorktreeCreateFailed, match="simulated"):
        await harness.service.create(session, PROJECT)

    assert _branches(repo) == {"main"}
    assert not (harness.directory / str(PROJECT) / str(session)).exists()
    assert harness.rows() == (0, 0)


async def test_a_cancel_mid_creation_still_undoes_everything(tmp_path: Path, repo: Path) -> None:
    (repo / "CLAUDE.md").write_text("instructions\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("untracked and not ignored\n", encoding="utf-8")
    harness = _Harness(tmp_path, repo)
    session = await harness.session()
    reached = asyncio.Event()

    async def hang(_worktree: Worktree, *, bound_to: SessionId) -> None:
        reached.set()
        await asyncio.Event().wait()

    harness.store.record = hang  # type: ignore[method-assign]
    task = asyncio.create_task(harness.service.create(session, PROJECT))
    await asyncio.wait_for(reached.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The shielded undo may still be finishing.
    for _ in range(200):
        if _branches(repo) == {"main"}:
            break
        await asyncio.sleep(0.05)

    assert _branches(repo) == {"main"}
    assert not (harness.directory / str(PROJECT) / str(session)).exists()
    assert harness.rows() == (0, 0)


async def test_a_branch_that_already_exists_is_refused_and_kept(tmp_path: Path, repo: Path) -> None:
    harness = _Harness(tmp_path, repo)
    session = await harness.session()
    branch = f"ra/{str(session)[:8]}"
    _git(repo, "branch", branch)

    with pytest.raises(WorktreeCreateFailed):
        await harness.service.create(session, PROJECT)

    assert branch in _branches(repo)
    assert harness.rows() == (0, 0)


# -- the launch command ------------------------------------------------------------------------


class _Terminal:
    def __init__(self, folders: SQLiteWorktreeStore, refuse: str = "") -> None:
        self.folders = folders
        self.refuse = refuse
        self.launched: list[tuple[SessionId, Worktree | None]] = []

    async def launch(self, session_id, project_id, profile_id, *, remote_control):  # type: ignore[no-untyped-def]
        self.launched.append((session_id, await self.folders.folder_for(session_id)))
        if self.refuse:
            return TerminalObservation(session_id, False, False, detail=self.refuse)
        return TerminalObservation(session_id, True, False)


async def test_a_worktree_launch_creates_and_binds_before_the_pane_starts(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo)
    terminal = _Terminal(harness.store)
    service = SessionService(
        harness.sessions,
        terminal,  # type: ignore[arg-type]
        worktrees=harness.store,
        worktree_service=harness.service,
    )

    outcome = await service.launch(LaunchCommand(PROJECT, CODEX, "k1", mode="worktree"))

    assert outcome.record.state is SessionState.RUNNING
    assert outcome.record.display.mode == "worktree"
    ((launched, folder),) = terminal.launched
    assert launched == outcome.record.session_id
    assert folder is not None and folder.branch == f"ra/{str(launched)[:8]}"

    regular = await service.launch(LaunchCommand(PROJECT, CODEX, "k2"))
    assert regular.record.display.mode == "regular"
    assert terminal.launched[-1][1] is None


async def test_a_failed_creation_fails_the_launch_with_git_s_reason_and_starts_nothing(
    tmp_path: Path, repo: Path
) -> None:
    harness = _Harness(tmp_path, repo, git=_AddThenFail())
    terminal = _Terminal(harness.store)
    service = SessionService(
        harness.sessions,
        terminal,  # type: ignore[arg-type]
        worktrees=harness.store,
        worktree_service=harness.service,
    )

    outcome = await service.launch(LaunchCommand(PROJECT, CODEX, "k1", mode="worktree"))

    assert outcome.record.state is SessionState.FAILED
    assert outcome.worktree_failed is not None
    assert "simulated failure" in outcome.worktree_failed
    assert terminal.launched == []


async def test_a_launch_refused_before_any_pane_takes_its_worktree_with_it(
    tmp_path: Path, repo: Path
) -> None:
    (repo / "CLAUDE.md").write_text("instructions\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("untracked and not ignored\n", encoding="utf-8")
    harness = _Harness(tmp_path, repo)
    terminal = _Terminal(harness.store, refuse=INVALID_INTENT)
    service = SessionService(
        harness.sessions,
        terminal,  # type: ignore[arg-type]
        worktrees=harness.store,
        worktree_service=harness.service,
    )

    outcome = await service.launch(LaunchCommand(PROJECT, CODEX, "k1", mode="worktree"))

    assert outcome.record.state is SessionState.FAILED
    ((launched, folder),) = terminal.launched
    assert folder is not None, "the worktree was made and bound before the terminal ran"
    assert _branches(repo) == {"main"}
    assert not folder.path.exists()
    gone = await harness.store.folder_for(launched)
    assert gone is not None and gone.removed_at is not None


def test_a_successor_never_creates_a_worktree() -> None:
    with pytest.raises(ValueError):
        LaunchCommand(PROJECT, CODEX, "k", mode="worktree", reuse_folder_of=SessionId.new())
