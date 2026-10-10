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
from remote_agents.ports.worktree_store import Worktree, WorktreeUnavailable

NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
PROJECT = ProjectId("remote-agents")
FOLDER = Path("/state/worktrees/remote-agents/s1")
# Canonical UUIDs, as every stored session id is: a store that rebuilt them any other way
# would answer with keys no session record equals.
S1 = "00000000-0000-4000-8000-000000000001"
S2 = "00000000-0000-4000-8000-000000000002"
S3 = "00000000-0000-4000-8000-000000000003"
S9 = "00000000-0000-4000-8000-000000000009"
NOBODY = "00000000-0000-4000-8000-0000000000ff"


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
    _session(before, S1)
    before.close()

    after = open_database(path)

    assert current_version(after) == 19
    assert after.execute("SELECT session_id FROM sessions").fetchall() == [(S1,)]
    assert after.execute("SELECT COUNT(*) FROM worktrees").fetchone() == (0,)
    assert after.execute("SELECT COUNT(*) FROM session_worktrees").fetchone() == (0,)
    assert current_version(sqlite3.connect(backup_path(path))) == 18


async def test_a_recorded_worktree_round_trips_and_answers_for_its_session(
    connection: sqlite3.Connection,
) -> None:
    store = SQLiteWorktreeStore(connection)
    _session(connection, S1)
    await store.record(_worktree(), bound_to=SessionId.parse(S1))

    assert await store.folder_for(SessionId.parse(S1)) == _worktree()
    assert await store.folder_for(SessionId.parse(NOBODY)) is None
    assert await store.live_for_project(PROJECT) == (_worktree(),)
    assert await store.live_for_project(ProjectId("other")) == ()
    assert await store.bound_sessions(FOLDER) == (SessionId.parse(S1),)


async def test_a_path_is_a_candidate_only_after_every_bound_session_ended(
    connection: sqlite3.Connection,
) -> None:
    store = SQLiteWorktreeStore(connection)
    _session(connection, S1)
    _session(connection, S2)
    await store.record(_worktree(), bound_to=SessionId.parse(S1))
    await store.bind(SessionId.parse(S2), FOLDER)

    assert await store.removable_candidates() == ()
    _end(connection, S1)
    assert await store.removable_candidates() == ()
    _end(connection, S2)
    assert await store.removable_candidates() == (_worktree(),)


async def test_a_session_still_failed_or_preserved_keeps_its_folder(
    connection: sqlite3.Connection,
) -> None:
    store = SQLiteWorktreeStore(connection)
    _session(connection, S1, state="failed")
    await store.record(_worktree(), bound_to=SessionId.parse(S1))
    assert await store.removable_candidates() == ()
    connection.execute("UPDATE sessions SET state = 'preserved' WHERE session_id = 's1'")
    connection.commit()
    assert await store.removable_candidates() == ()


async def test_mark_removed_takes_the_path_out_of_every_live_answer(
    connection: sqlite3.Connection,
) -> None:
    store = SQLiteWorktreeStore(connection)
    _session(connection, S1, state="ended")
    await store.record(_worktree(), bound_to=SessionId.parse(S1))
    await store.mark_removed(FOLDER, at=NOW)

    assert await store.live_for_project(PROJECT) == ()
    assert await store.removable_candidates() == ()
    bound = await store.folder_for(SessionId.parse(S1))
    assert bound is not None and bound.removed_at == NOW


async def test_binding_a_session_twice_to_another_folder_is_refused(
    connection: sqlite3.Connection,
) -> None:
    store = SQLiteWorktreeStore(connection)
    _session(connection, S1)
    _session(connection, S9)
    other = Path("/state/worktrees/remote-agents/s9")
    await store.record(_worktree(), bound_to=SessionId.parse(S1))
    await store.record(_worktree(path=other), bound_to=SessionId.parse(S9))
    await store.bind(SessionId.parse(S1), FOLDER)
    with pytest.raises(ValueError):
        await store.bind(SessionId.parse(S1), other)
    assert (await store.folder_for(SessionId.parse(S1))) == _worktree()


async def test_a_record_whose_binding_fails_leaves_no_folder_row(
    connection: sqlite3.Connection,
) -> None:
    """The row and its first binding are one transaction: no reader may see a folder nobody
    is bound to, because a fresh folder reads as merged and clean."""
    store = SQLiteWorktreeStore(connection)
    with pytest.raises(ValueError):
        await store.record(_worktree(), bound_to=SessionId("no-such-session"))
    assert connection.execute("SELECT COUNT(*) FROM worktrees").fetchone() == (0,)
    assert await store.removable_candidates() == ()


async def test_a_folder_with_no_binding_is_never_a_candidate(
    connection: sqlite3.Connection,
) -> None:
    """Written past the store, the way a defect elsewhere would leave one."""
    store = SQLiteWorktreeStore(connection)
    connection.execute(
        "INSERT INTO worktrees(path, project_id, branch, base_branch, base_commit, copied,"
        " created_at) VALUES (?, 'p', 'ra/x', 'main', 'c', '{}', '2026-10-10T00:00:00+00:00')",
        (str(FOLDER),),
    )
    connection.commit()
    assert await store.removable_candidates() == ()
    assert await store.claim_for_removal(FOLDER, at=NOW) is False


async def test_a_detached_base_is_never_a_candidate(connection: sqlite3.Connection) -> None:
    store = SQLiteWorktreeStore(connection)
    _session(connection, S1, state="ended")
    detached = Worktree(**{**_fields(_worktree()), "base_branch": None})
    await store.record(detached, bound_to=SessionId.parse(S1))
    assert await store.removable_candidates() == ()
    assert await store.claim_for_removal(FOLDER, at=NOW) is False


async def test_a_claim_and_a_bind_exclude_each_other(connection: sqlite3.Connection) -> None:
    """The resume-during-removal race: whichever lands first wins, the other is refused."""
    store = SQLiteWorktreeStore(connection)
    _session(connection, S1, state="ended")
    _session(connection, S2)
    await store.record(_worktree(), bound_to=SessionId.parse(S1))

    assert await store.claim_for_removal(FOLDER, at=NOW) is True
    assert await store.removable_candidates() == ()
    with pytest.raises(WorktreeUnavailable):
        await store.bind(SessionId.parse(S2), FOLDER)
    assert await store.bound_sessions(FOLDER) == (SessionId.parse(S1),)

    await store.release_claim(FOLDER)
    await store.bind(SessionId.parse(S2), FOLDER)
    assert await store.claim_for_removal(FOLDER, at=NOW) is False


async def test_a_second_claim_and_a_bind_to_a_removed_folder_are_refused(
    connection: sqlite3.Connection,
) -> None:
    store = SQLiteWorktreeStore(connection)
    _session(connection, S1, state="ended")
    _session(connection, S2)
    await store.record(_worktree(), bound_to=SessionId.parse(S1))
    assert await store.claim_for_removal(FOLDER, at=NOW) is True
    assert await store.claim_for_removal(FOLDER, at=NOW) is False
    await store.mark_removed(FOLDER, at=NOW)
    with pytest.raises(WorktreeUnavailable):
        await store.bind(SessionId.parse(S2), FOLDER)
    with pytest.raises(WorktreeUnavailable):
        await store.bind(SessionId.parse(S2), Path("/never/recorded"))


async def test_stale_claims_are_released_and_removed_folders_keep_theirs(
    connection: sqlite3.Connection,
) -> None:
    store = SQLiteWorktreeStore(connection)
    _session(connection, S1, state="ended")
    _session(connection, S2, state="ended")
    other = Path("/state/worktrees/remote-agents/s2")
    await store.record(_worktree(), bound_to=SessionId.parse(S1))
    await store.record(_worktree(path=other), bound_to=SessionId.parse(S2))
    assert await store.claim_for_removal(FOLDER, at=NOW) is True
    assert await store.claim_for_removal(other, at=NOW) is True
    await store.mark_removed(other, at=NOW)

    assert await store.release_stale_claims() == 1
    assert await store.removable_candidates() == (_worktree(),)


def _fields(worktree: Worktree) -> dict[str, object]:
    return {name: getattr(worktree, name) for name in Worktree.__slots__}


async def test_every_binding_is_read_at_once_removed_folders_included(
    connection: sqlite3.Connection,
) -> None:
    """One read for a pass over every session; a removed folder still answers for the sessions
    bound to it, so none of them is mistaken for a regular session."""
    store = SQLiteWorktreeStore(connection)
    for session in (S1, S2, S3):
        _session(connection, session)
    gone = Path("/state/worktrees/remote-agents/gone")
    await store.record(_worktree(), bound_to=SessionId.parse(S1))
    await store.bind(SessionId.parse(S2), FOLDER)
    await store.record(_worktree(gone), bound_to=SessionId.parse(S3))
    await store.mark_removed(gone, at=NOW)

    bound = await store.bound_folders()

    assert {session: worktree.path for session, worktree in bound.items()} == {
        SessionId.parse(S1): FOLDER,
        SessionId.parse(S2): FOLDER,
        SessionId.parse(S3): gone,
    }
    assert bound[SessionId.parse(S3)].removed_at is not None
