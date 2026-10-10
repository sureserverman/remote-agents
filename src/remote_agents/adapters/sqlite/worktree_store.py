"""Session worktrees in the domain store (migration 19), beside the sessions they bind.

"Ended" is the domain's own `TERMINAL_STATES`, spelled into the candidate query once at import:
a FAILED or PRESERVED session can still be stopped or resumed, so it still holds its folder.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from remote_agents.domain.models import ProjectId, SessionId
from remote_agents.domain.state_machine import TERMINAL_STATES
from remote_agents.ports.worktree_store import Worktree

_COLUMNS = "path, project_id, branch, base_branch, base_commit, copied, created_at, removed_at"
_ENDED = ", ".join(f"'{state.value}'" for state in sorted(TERMINAL_STATES))


class SQLiteWorktreeStore:
    """`WorktreeStore` over the domain database the session store writes."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    async def record(self, worktree: Worktree, *, bound_to: SessionId | None) -> None:
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
                    worktree.created_at.isoformat(),
                    worktree.removed_at.isoformat() if worktree.removed_at else None,
                ),
            )
            if bound_to is not None:
                self._insert_binding(bound_to, worktree.path)

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
        self._connection.execute(
            "INSERT INTO session_worktrees(session_id, worktree_path) VALUES (?, ?)",
            (str(session_id), str(path)),
        )

    async def folder_for(self, session_id: SessionId) -> Worktree | None:
        rows = self._select(
            "path = (SELECT worktree_path FROM session_worktrees WHERE session_id = ?)",
            (str(session_id),),
        )
        return rows[0] if rows else None

    async def live_for_project(self, project_id: ProjectId) -> tuple[Worktree, ...]:
        return self._select("project_id = ? AND removed_at IS NULL", (str(project_id),))

    async def bound_sessions(self, path: Path) -> tuple[SessionId, ...]:
        rows = self._connection.execute(
            "SELECT session_id FROM session_worktrees WHERE worktree_path = ? ORDER BY session_id",
            (str(path),),
        ).fetchall()
        return tuple(SessionId(row[0]) for row in rows)

    async def removable_candidates(self) -> tuple[Worktree, ...]:
        return self._select(
            "removed_at IS NULL AND NOT EXISTS ("
            " SELECT 1 FROM session_worktrees AS b JOIN sessions AS s"
            " ON s.session_id = b.session_id"
            f" WHERE b.worktree_path = worktrees.path AND s.state NOT IN ({_ENDED}))",
            (),
        )

    async def mark_removed(self, path: Path, *, at: datetime) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE worktrees SET removed_at = ? WHERE path = ? AND removed_at IS NULL",
                (at.isoformat(), str(path)),
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
