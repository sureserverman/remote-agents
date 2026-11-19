"""A worktree waiting for a deferred resume is in use (BL-138 item 1, DEC-124).

A restart that defers a resume -- the Codex daemon guard refused it -- leaves the session ENDED
with its outcome `resuming`, so the next start can try again in that session's own folder. The
cleanup pass must not remove the folder in between: merged and clean is not unreferenced while
a resume still means to return to it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.application.worktree_cleanup import IN_USE, WorktreeCleanupPass
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.interrupted_resumes import RESUMING
from remote_agents.ports.worktree_store import Worktree

PROJECT = ProjectId("editor")
AT = datetime(2026, 11, 19, tzinfo=UTC)


@dataclass(frozen=True)
class _Head:
    branch: str


class _Git:
    """A merged, clean worktree whose entry git still holds."""

    def __init__(self) -> None:
        self.removed: list[Path] = []

    async def worktree_head(self, repo: Path, path: Path) -> _Head | None:
        return None if path in self.removed else _Head("ra/session")

    async def has_branch(self, repo: Path, branch: str) -> bool:
        return False

    async def is_ancestor(self, repo: Path, commit: str, ref: str) -> bool:
        return True

    async def unmerged_history(self, repo: Path, path: Path, branch: str, base: str) -> int:
        return 0

    async def status_lines(self, path: Path) -> tuple[str, ...]:
        return ()

    async def remove_worktree(self, repo: Path, path: Path) -> None:
        self.removed.append(path)
        path.rmdir()

    async def delete_merged_branch(self, repo: Path, branch: str) -> None:
        raise AssertionError("has_branch said there is none")


class _Store:
    def __init__(self, worktree: Worktree, session: SessionId) -> None:
        self.worktree = worktree
        self.session = session
        self.claimed = False

    async def removable_candidates(self) -> tuple[Worktree, ...]:
        return () if self.worktree.removed_at else (self.worktree,)

    async def bound_sessions(self, path: Path) -> tuple[SessionId, ...]:
        return (self.session,)

    async def bound_folders(self):
        return {self.session: self.worktree}

    async def claim_for_removal(self, path: Path, *, at: datetime) -> bool:
        self.claimed = True
        return True

    async def release_claim(self, path: Path) -> None:
        self.claimed = False

    async def release_stale_claims(self) -> int:
        return 0

    async def mark_removed(self, path: Path, *, at: datetime) -> None:
        self.worktree = replace(self.worktree, removed_at=at)


class _Sessions:
    def __init__(self, record: SessionRecord) -> None:
        self.record = record

    async def get(self, session_id: SessionId) -> SessionRecord | None:
        return self.record if session_id == self.record.session_id else None


class _Rollovers:
    async def open_rollovers(self):
        return ()


class _Resumes:
    def __init__(self, rows: dict[str, str]) -> None:
        self.rows = rows

    async def outcomes(self) -> dict[str, str]:
        return dict(self.rows)


def _pass(tmp_path: Path, rows: dict[str, str]) -> tuple[WorktreeCleanupPass, Worktree]:
    session = SessionId.new()
    folder = tmp_path / "worktrees" / "editor" / "session"
    folder.mkdir(parents=True)
    worktree = Worktree(folder, PROJECT, "ra/session", "main", "0" * 40, {}, AT, None)
    record = SessionRecord(
        session,
        PROJECT,
        ProfileId("codex"),
        SessionDisplayIdentity("editor", "codex", "regular", 1),
        SessionState.ENDED,
        AT,
    )
    resumes = _Resumes({key.format(s=session): value for key, value in rows.items()})

    async def repository(_project: ProjectId) -> Path:
        return tmp_path / "repo"

    cleanup = WorktreeCleanupPass(
        git=_Git(),
        store=_Store(worktree, session),
        sessions=_Sessions(record),
        rollovers=_Rollovers(),
        repository=repository,
        resumes=resumes,
    )
    return cleanup, worktree


async def test_a_worktree_whose_session_waits_for_a_deferred_resume_is_in_use(
    tmp_path: Path,
) -> None:
    cleanup, worktree = _pass(tmp_path, {"{s}": RESUMING})

    assert await cleanup.verdict(worktree) == IN_USE
    report = await cleanup.pass_once()

    assert report.removed == ()
    assert report.kept == {worktree.path: IN_USE}
    assert worktree.path.is_dir()


@pytest.mark.parametrize("rows", [{"{s}": "resumed:abc"}, {"{s}": "skipped:no-conversation"}, {}])
async def test_a_resumed_or_unrecorded_session_leaves_its_worktree_removable(
    tmp_path: Path, rows: dict[str, str]
) -> None:
    cleanup, worktree = _pass(tmp_path, rows)

    assert await cleanup.verdict(worktree) is None
    report = await cleanup.pass_once()

    assert report.removed == (worktree.path,)
    assert not worktree.path.exists()
