"""Where a worktree session's handoff envelopes are read (BL-116, DEC-123): under the worktree's
own top level, or nowhere -- never a folder above it, and never the project's checkout."""

from __future__ import annotations

import sqlite3
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import adopt_templates
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
from remote_agents.composition.service import build_rollover_pass
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.ports.worktree_store import Worktree

_PROJECT = ProjectId("p")
_NOW = datetime(2026, 10, 10, tzinfo=UTC)


def _session(connection: sqlite3.Connection, session_id: str) -> None:
    connection.execute(
        "INSERT INTO sessions(session_id, project_id, profile_id, display_identity, state,"
        " created_at) VALUES (?, 'p', 'claude', '{}', 'running', ?)",
        (session_id, _NOW.isoformat()),
    )
    connection.commit()


def _roots(connection: sqlite3.Connection, project: Path, folders=None):
    rollover = build_rollover_pass(
        connection=connection,
        sessions=types.SimpleNamespace(list_sessions=None, launch=None, graceful_stop=None),
        terminal=types.SimpleNamespace(pane_idle=None, send_prompt=None),
        enabled=None,
        project_paths={_PROJECT: project},
        adopt_templates={ProfileId("claude"): adopt_templates()[ProfileId("claude")]},
        worktrees=SQLiteWorktreeStore(connection),
        folders=folders,
    )
    return rollover._worktree_roots()


async def _bound(connection, folder: Path) -> SessionId:
    session = SessionId.new()
    _session(connection, str(session))
    await SQLiteWorktreeStore(connection).record(
        Worktree(folder, _PROJECT, "ra/x", "main", "a" * 40, {}, _NOW, None), bound_to=session
    )
    return session


@pytest.mark.asyncio
async def test_a_worktree_with_its_git_file_is_its_own_root(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "s.sqlite3")
    folder = tmp_path / "state" / "wt"
    folder.mkdir(parents=True)
    (folder / ".git").write_text("gitdir: /elsewhere\n")
    session = await _bound(connection, folder)

    assert await _roots(connection, tmp_path / "project") == {session: folder}


@pytest.mark.asyncio
async def test_a_worktree_without_its_git_file_has_no_root_even_under_a_repository(
    tmp_path: Path,
) -> None:
    """A folder that lost its `.git` is not a checkout. Walking up from it can reach any
    repository above the state folder -- a home directory kept in git -- whose envelopes are
    not this session's."""
    connection = open_database(tmp_path / "s.sqlite3")
    (tmp_path / ".git").mkdir()
    folder = tmp_path / "state" / "wt"
    folder.mkdir(parents=True)
    session = await _bound(connection, folder)

    assert await _roots(connection, tmp_path / "project") == {session: None}


@pytest.mark.asyncio
async def test_a_binding_row_that_cannot_be_read_does_not_stop_the_others(
    tmp_path: Path,
) -> None:
    connection = open_database(tmp_path / "s.sqlite3")
    folder = tmp_path / "state" / "wt"
    folder.mkdir(parents=True)
    (folder / ".git").write_text("gitdir: /elsewhere\n")
    good = await _bound(connection, folder)
    _session(connection, "not-a-uuid")
    connection.execute(
        "INSERT INTO session_worktrees(session_id, worktree_path) VALUES ('not-a-uuid', ?)",
        (str(folder),),
    )
    connection.commit()

    assert await _roots(connection, tmp_path / "project") == {good: folder}


@pytest.mark.asyncio
async def test_a_session_bound_to_a_folder_has_that_folder_as_its_default_root(
    tmp_path: Path,
) -> None:
    """A successor started outside its project (DEC-131) reads its envelopes there with no
    report of its own; once the folder is no longer a top level it has no root at all."""
    import subprocess

    from remote_agents.adapters.sqlite.folder_bindings import SQLiteFolderBindings
    from remote_agents.adapters.workflow.roots import acceptable_root

    connection = open_database(tmp_path / "s.sqlite3")
    folders = SQLiteFolderBindings(connection, accept=acceptable_root)
    there = tmp_path / "there"
    there.mkdir()
    subprocess.run(["git", "init", "-q", "--template=", str(there)], check=True, timeout=60)
    session = SessionId.new()
    _session(connection, str(session))
    assert await folders.bind(session, there)

    assert await _roots(connection, tmp_path / "project", folders) == {session: there.resolve()}

    (there / ".git").rename(there / "was-git")
    assert await _roots(connection, tmp_path / "project", folders) == {session: None}
