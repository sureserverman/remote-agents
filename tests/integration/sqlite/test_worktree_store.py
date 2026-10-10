"""Session worktrees in the domain store (migration 19): one row per folder, sessions bound to
it, and a folder that becomes a removal candidate only once every session bound to it ended."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import backup_path, open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS, current_version
from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
from remote_agents.domain.models import ProjectId, SessionId
from remote_agents.ports.worktree_store import Worktree

NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
PROJECT = ProjectId("remote-agents")
FOLDER = Path("/state/worktrees/remote-agents/s1")


def _session(connection: sqlite3.Connection, session_id: str, state: str = "running") -> None:
    connection.execute(
        "INSERT INTO sessions(session_id, project_id, profile_id, display_identity, state,"
        " created_at) VALUES (?, ?, 'codex', '{}', ?, '2026-10-10T00:00:00+00:00')",
        (session_id, str(PROJECT), state),
    )
    connection.commit()


def _end(connection: sqlite3.Connection, session_id: str) -> None:
    connection.execute("UPDATE sessions SET state = 'ended' WHERE session_id = ?", (session_id,))
    connection.commit()


def _worktree(path: Path = FOLDER, project: ProjectId = PROJECT) -> Worktree:
    return Worktree(
        path=path,
        project_id=project,
        branch="ra/s1",
        base_branch="main",
        base_commit="a" * 40,
        copied={"CLAUDE.md": "f" * 64},
        created_at=NOW,
        removed_at=None,
    )


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    return open_database(tmp_path / "sessions.sqlite3")


def test_an_existing_store_migrates_to_19_with_every_session_kept(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    before = open_database(path, migrations=tuple(m for m in MIGRATIONS if m[0] < 19))
    _session(before, "s1")
    before.close()

    after = open_database(path)

    assert current_version(after) == 19
    assert after.execute("SELECT session_id FROM sessions").fetchall() == [("s1",)]
    assert after.execute("SELECT COUNT(*) FROM worktrees").fetchone() == (0,)
    assert after.execute("SELECT COUNT(*) FROM session_worktrees").fetchone() == (0,)
    assert current_version(sqlite3.connect(backup_path(path))) == 18


async def test_a_recorded_worktree_round_trips_and_answers_for_its_session(
    connection: sqlite3.Connection,
) -> None:
    store = SQLiteWorktreeStore(connection)
    _session(connection, "s1")
    await store.record(_worktree(), bound_to=SessionId("s1"))

    assert await store.folder_for(SessionId("s1")) == _worktree()
    assert await store.folder_for(SessionId("nobody")) is None
    assert await store.live_for_project(PROJECT) == (_worktree(),)
    assert await store.live_for_project(ProjectId("other")) == ()
    assert await store.bound_sessions(FOLDER) == (SessionId("s1"),)


async def test_a_path_is_a_candidate_only_after_every_bound_session_ended(
    connection: sqlite3.Connection,
) -> None:
    store = SQLiteWorktreeStore(connection)
    _session(connection, "s1")
    _session(connection, "s2")
    await store.record(_worktree(), bound_to=SessionId("s1"))
    await store.bind(SessionId("s2"), FOLDER)

    assert await store.removable_candidates() == ()
    _end(connection, "s1")
    assert await store.removable_candidates() == ()
    _end(connection, "s2")
    assert await store.removable_candidates() == (_worktree(),)


async def test_a_session_still_failed_or_preserved_keeps_its_folder(
    connection: sqlite3.Connection,
) -> None:
    store = SQLiteWorktreeStore(connection)
    _session(connection, "s1", state="failed")
    await store.record(_worktree(), bound_to=SessionId("s1"))
    assert await store.removable_candidates() == ()
    connection.execute("UPDATE sessions SET state = 'preserved' WHERE session_id = 's1'")
    connection.commit()
    assert await store.removable_candidates() == ()


async def test_mark_removed_takes_the_path_out_of_every_live_answer(
    connection: sqlite3.Connection,
) -> None:
    store = SQLiteWorktreeStore(connection)
    _session(connection, "s1", state="ended")
    await store.record(_worktree(), bound_to=SessionId("s1"))
    await store.mark_removed(FOLDER, at=NOW)

    assert await store.live_for_project(PROJECT) == ()
    assert await store.removable_candidates() == ()
    bound = await store.folder_for(SessionId("s1"))
    assert bound is not None and bound.removed_at == NOW


async def test_binding_a_session_twice_to_another_folder_is_refused(
    connection: sqlite3.Connection,
) -> None:
    store = SQLiteWorktreeStore(connection)
    _session(connection, "s1")
    other = Path("/state/worktrees/remote-agents/s9")
    await store.record(_worktree(), bound_to=SessionId("s1"))
    await store.record(_worktree(path=other), bound_to=None)
    await store.bind(SessionId("s1"), FOLDER)
    with pytest.raises(ValueError):
        await store.bind(SessionId("s1"), other)
    assert (await store.folder_for(SessionId("s1"))) == _worktree()
