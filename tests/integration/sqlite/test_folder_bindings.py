"""A session bound to a folder outside its project (migration 21, DEC-131).

A rollover successor of a session whose handoff writer reported another git top level starts
there. The binding is its own table, never a column on `sessions` (DEC-115's reason: a session
row is rebuilt by position), and it holds only a folder the one acceptance rule passes.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.folder_bindings import SQLiteFolderBindings
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.workflow.roots import acceptable_root
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


def _top_level(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--template=", str(path)], check=True, timeout=60)
    return path.resolve()


async def _session(connection) -> SessionId:
    record = SessionRecord(
        SessionId.new(),
        ProjectId("editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("editor", "claude", "regular", 1),
        SessionState.STARTING,
        datetime(2026, 10, 22, tzinfo=UTC),
    )
    await SQLiteSessionStore(connection).save(record)
    return record.session_id


@pytest.mark.asyncio
async def test_a_bound_folder_is_answered_and_survives_a_reopen(tmp_path) -> None:
    database = tmp_path / "sessions.sqlite3"
    connection = open_database(database)
    session = await _session(connection)
    there = _top_level(tmp_path / "there")

    assert await SQLiteFolderBindings(connection, accept=acceptable_root).bind(session, there)
    connection.close()

    reopened = SQLiteFolderBindings(open_database(database), accept=acceptable_root)
    assert await reopened.folder_for(session) == there
    assert await reopened.bound_folders() == {session: there}


@pytest.mark.asyncio
async def test_a_folder_the_rule_refuses_is_never_bound(tmp_path) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")
    session = await _session(connection)
    there = _top_level(tmp_path / "there")
    (there / "sub").mkdir()
    bindings = SQLiteFolderBindings(connection, accept=acceptable_root)

    for refused in (there / "sub", tmp_path / "missing", Path("relative")):
        assert not await bindings.bind(session, refused)

    assert await bindings.folder_for(session) is None
    assert await bindings.bound_folders() == {}


@pytest.mark.asyncio
async def test_an_unbound_session_has_no_folder(tmp_path) -> None:
    bindings = SQLiteFolderBindings(
        open_database(tmp_path / "sessions.sqlite3"), accept=acceptable_root
    )

    assert await bindings.folder_for(SessionId.new()) is None
