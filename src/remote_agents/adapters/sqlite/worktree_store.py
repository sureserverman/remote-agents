"""Session worktrees in the domain store (migration 19), beside the sessions they bind.

"Ended" is the domain's own `TERMINAL_STATES`, spelled into the queries once at import: a
FAILED or PRESERVED session can still be stopped or resumed, so it still holds its folder. A
binding whose session row is missing counts as live, so the queries fail toward keeping.

`bind` and `claim_for_removal` are each a single conditional statement, so they exclude each
other across processes without a transaction held over a read (`ports/worktree_store.py`).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from remote_agents.domain.models import ProjectId, SessionId
from remote_agents.domain.state_machine import TERMINAL_STATES
from remote_agents.ports.worktree_store import Worktree, WorktreeUnavailable

_COLUMNS = "path, project_id, branch, base_branch, base_commit, copied, created_at, removed_at"
_ENDED = ", ".join(f"'{state.value}'" for state in sorted(TERMINAL_STATES))
_HELD = (
    "EXISTS (SELECT 1 FROM session_worktrees AS b LEFT JOIN sessions AS s"
    " ON s.session_id = b.session_id WHERE b.worktree_path = worktrees.path"
    f" AND (s.state IS NULL OR s.state NOT IN ({_ENDED})))"
)
"""A live session is bound to the folder, or a binding names a session row that is missing."""
_CANDIDATE = (
    "removed_at IS NULL AND removing_at IS NULL AND base_branch IS NOT NULL"
    " AND EXISTS (SELECT 1 FROM session_worktrees AS b WHERE b.worktree_path = worktrees.path)"
    f" AND NOT {_HELD}"
)


def _utc(moment: datetime) -> str:
    """Stored in UTC, so `ORDER BY created_at` over the text is time order."""
    return moment.astimezone(UTC).isoformat()


class SQLiteWorktreeStore:
    """`WorktreeStore` over the domain database the session store writes."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    async def record(self, worktree: Worktree, *, bound_to: SessionId) -> None:
        try:
            with self._connection:
                self._connection.execute(
                    f"INSERT INTO worktrees({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(worktree.path),
                        str(worktree.project_id),
                        worktree.branch,
                        worktree.base_branch,
                        worktree.base_commit,
                        json.dumps(dict(worktree.copied), sort_keys=True),
                        _utc(worktree.created_at),
                        _utc(worktree.removed_at) if worktree.removed_at else None,
                    ),
                )
                self._insert_binding(bound_to, worktree.path)
        except sqlite3.IntegrityError as error:
            raise ValueError(f"worktree not recorded: {error}") from None

    async def bind(self, session_id: SessionId, path: Path) -> None:
        with self._connection:
            self._insert_binding(session_id, path)

    def _insert_binding(self, session_id: SessionId, path: Path) -> None:
        current = self._connection.execute(
            "SELECT worktree_path FROM session_worktrees WHERE session_id = ?",
            (str(session_id),),
        ).fetchone()
        if current is not None:
            if current[0] != str(path):
                raise ValueError(f"session {session_id} is already bound to another worktree")
            return
        try:
            # One statement: the folder must be live and unclaimed at the instant of the write.
            inserted = self._connection.execute(
                "INSERT INTO session_worktrees(session_id, worktree_path)"
                " SELECT ?, path FROM worktrees"
                " WHERE path = ? AND removed_at IS NULL AND removing_at IS NULL",
                (str(session_id), str(path)),
            ).rowcount
        except sqlite3.IntegrityError as error:
            raise ValueError(f"session {session_id} cannot be bound: {error}") from None
        if inserted != 1:
            raise WorktreeUnavailable(f"{path} is not a live, unclaimed worktree")

    async def folder_for(self, session_id: SessionId) -> Worktree | None:
        rows = self._select(
            "path = (SELECT worktree_path FROM session_worktrees WHERE session_id = ?)",
            (str(session_id),),
        )
        return rows[0] if rows else None

    async def live_for_project(self, project_id: ProjectId) -> tuple[Worktree, ...]:
        return self._select("project_id = ? AND removed_at IS NULL", (str(project_id),))

    async def bound_folders(self) -> dict[SessionId, Worktree]:
        rows = self._connection.execute(
            f"SELECT session_worktrees.session_id, {_COLUMNS} FROM session_worktrees"
            " JOIN worktrees ON worktrees.path = session_worktrees.worktree_path"
        ).fetchall()
        return {SessionId.parse(row[0]): _worktree(row[1:]) for row in rows}

    async def bound_sessions(self, path: Path) -> tuple[SessionId, ...]:
        rows = self._connection.execute(
            "SELECT session_id FROM session_worktrees WHERE worktree_path = ? ORDER BY session_id",
            (str(path),),
        ).fetchall()
        return tuple(SessionId.parse(row[0]) for row in rows)

    async def removable_candidates(self) -> tuple[Worktree, ...]:
        return self._select(_CANDIDATE, ())

    async def claim_for_removal(self, path: Path, *, at: datetime) -> bool:
        with self._connection:
            claimed = self._connection.execute(
                f"UPDATE worktrees SET removing_at = ? WHERE path = ? AND {_CANDIDATE}",
                (_utc(at), str(path)),
            ).rowcount
        return claimed == 1

    async def release_claim(self, path: Path) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE worktrees SET removing_at = NULL WHERE path = ? AND removed_at IS NULL",
                (str(path),),
            )

    async def release_stale_claims(self) -> int:
        with self._connection:
            return self._connection.execute(
                "UPDATE worktrees SET removing_at = NULL"
                " WHERE removing_at IS NOT NULL AND removed_at IS NULL"
            ).rowcount

    async def mark_removed(self, path: Path, *, at: datetime) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE worktrees SET removed_at = ? WHERE path = ? AND removed_at IS NULL",
                (_utc(at), str(path)),
            )

    def _select(self, where: str, parameters: tuple[str, ...]) -> tuple[Worktree, ...]:
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM worktrees WHERE {where} ORDER BY created_at, path",
            parameters,
        ).fetchall()
        return tuple(_worktree(row) for row in rows)


def _worktree(row: tuple) -> Worktree:
    path, project_id, branch, base_branch, base_commit, copied, created_at, removed_at = row
    return Worktree(
        path=Path(path),
        project_id=ProjectId(project_id),
        branch=branch,
        base_branch=base_branch,
        base_commit=base_commit,
        copied=json.loads(copied),
        created_at=datetime.fromisoformat(created_at),
        removed_at=datetime.fromisoformat(removed_at) if removed_at else None,
    )
