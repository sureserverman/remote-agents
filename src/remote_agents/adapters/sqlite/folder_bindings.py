"""Sessions bound to a folder outside their project, in the domain store (migration 21)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from remote_agents.domain.models import SessionId

_LOG = logging.getLogger(__name__)


class SQLiteFolderBindings:
    """`FolderBindings` over the domain database the session store writes.

    `accept` is the one acceptance rule a reported root passes (`adapters/workflow/roots.py`
    `acceptable_root`), handed in by the composition root because an adapter here may not
    import another adapter. What it answers -- the resolved top level -- is what is stored.
    """

    def __init__(
        self, connection: sqlite3.Connection, *, accept: Callable[[object], Path | None]
    ) -> None:
        self._connection = connection
        self._accept = accept

    async def bind(self, session_id: SessionId, folder: Path) -> bool:
        accepted = self._accept(str(folder))
        if accepted is None:
            return False
        with self._connection:
            self._connection.execute(
                "INSERT INTO session_folders(session_id, folder) VALUES (?, ?)",
                (str(session_id), str(accepted)),
            )
        return True

    async def folder_for(self, session_id: SessionId) -> Path | None:
        row = self._connection.execute(
            "SELECT folder FROM session_folders WHERE session_id = ?", (str(session_id),)
        ).fetchone()
        return None if row is None else Path(row[0])

    async def bound_folders(self) -> dict[SessionId, Path]:
        bound: dict[SessionId, Path] = {}
        for session, folder in self._connection.execute(
            "SELECT session_id, folder FROM session_folders"
        ).fetchall():
            try:
                session_id = SessionId.parse(session)
            except ValueError:
                _LOG.warning("skipping a folder binding whose session id is not a UUID")
                continue
            bound[session_id] = Path(folder)
        return bound
