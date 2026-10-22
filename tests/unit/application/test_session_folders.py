"""One answer to "which folder does this session work in" (BL-116).

A regular session works in its project's folder. A session bound to a worktree works in that
worktree, and once the worktree is gone it has no folder at all: the lookup never falls back
to the main checkout, where the agent would land on the work it was given its own folder to
keep away from.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.application.session_folders import SessionFolders
from remote_agents.domain.models import ProjectId, SessionId
from remote_agents.ports.worktree_store import Worktree

_PROJECT = ProjectId("opaque-editor")


class _Store:
    """The one store read the lookup makes."""

    def __init__(self, bound: dict[SessionId, Worktree]) -> None:
        self._bound = bound

    async def folder_for(self, session_id: SessionId) -> Worktree | None:
        return self._bound.get(session_id)


def _worktree(path: Path, *, removed: bool = False) -> Worktree:
    return Worktree(
        path=path,
        project_id=_PROJECT,
        branch="ra/0123abcd",
        base_branch="main",
        base_commit="0" * 40,
        copied={},
        created_at=datetime(2026, 10, 10, tzinfo=UTC),
        removed_at=datetime(2026, 10, 11, tzinfo=UTC) if removed else None,
    )


@pytest.mark.asyncio
async def test_an_unbound_session_works_in_its_project_folder(tmp_path) -> None:
    folders = SessionFolders({_PROJECT: tmp_path}, _Store({}))

    assert await folders.folder_for(SessionId.new(), _PROJECT) == tmp_path


@pytest.mark.asyncio
async def test_a_lookup_with_no_store_is_the_project_folder(tmp_path) -> None:
    """A composition with no worktree rows (a test, a tool) still answers regular sessions."""
    folders = SessionFolders({_PROJECT: tmp_path}, None)

    assert await folders.folder_for(SessionId.new(), _PROJECT) == tmp_path


@pytest.mark.asyncio
async def test_a_bound_session_works_in_its_worktree(tmp_path) -> None:
    session = SessionId.new()
    worktree = tmp_path / "worktrees" / "one"
    worktree.mkdir(parents=True)
    folders = SessionFolders({_PROJECT: tmp_path}, _Store({session: _worktree(worktree)}))

    assert await folders.folder_for(session, _PROJECT) == worktree


@pytest.mark.asyncio
async def test_a_bound_worktree_deleted_on_disk_has_no_folder(tmp_path) -> None:
    session = SessionId.new()
    gone = tmp_path / "worktrees" / "gone"
    folders = SessionFolders({_PROJECT: tmp_path}, _Store({session: _worktree(gone)}))

    assert await folders.folder_for(session, _PROJECT) is None


@pytest.mark.asyncio
async def test_a_bound_worktree_marked_removed_has_no_folder(tmp_path) -> None:
    """Marked removed is gone even if a folder of that name has since reappeared."""
    session = SessionId.new()
    worktree = tmp_path / "worktrees" / "reused-name"
    worktree.mkdir(parents=True)
    folders = SessionFolders(
        {_PROJECT: tmp_path}, _Store({session: _worktree(worktree, removed=True)})
    )

    assert await folders.folder_for(session, _PROJECT) is None


@pytest.mark.asyncio
async def test_a_worktree_path_that_is_a_file_has_no_folder(tmp_path) -> None:
    session = SessionId.new()
    planted = tmp_path / "not-a-folder"
    planted.write_text("x", encoding="utf-8")
    folders = SessionFolders({_PROJECT: tmp_path}, _Store({session: _worktree(planted)}))

    assert await folders.folder_for(session, _PROJECT) is None


@pytest.mark.asyncio
async def test_an_unknown_project_is_refused_as_the_mapping_refuses_it(tmp_path) -> None:
    folders = SessionFolders({}, _Store({}))

    with pytest.raises(KeyError):
        await folders.folder_for(SessionId.new(), _PROJECT)


@pytest.mark.asyncio
async def test_the_lookup_reads_the_live_mapping_not_a_copy(tmp_path) -> None:
    """The catalogue mapping is refreshed in place; a project added later is found."""
    paths: dict[ProjectId, Path] = {}
    folders = SessionFolders(paths, _Store({}))
    paths[_PROJECT] = tmp_path

    assert await folders.folder_for(SessionId.new(), _PROJECT) == tmp_path


class _Folders:
    """The folder bindings the lookup reads after the worktree one (DEC-131)."""

    def __init__(self, bound: dict[SessionId, Path]) -> None:
        self._bound = bound

    async def folder_for(self, session_id: SessionId) -> Path | None:
        return self._bound.get(session_id)


@pytest.mark.asyncio
async def test_a_session_bound_to_a_folder_works_in_it(tmp_path) -> None:
    session = SessionId.new()
    there = tmp_path / "there"
    there.mkdir()
    folders = SessionFolders(
        {_PROJECT: tmp_path / "project"}, _Store({}), _Folders({session: there})
    )

    assert await folders.folder_for(session, _PROJECT) == there


@pytest.mark.asyncio
async def test_a_session_whose_bound_folder_is_gone_has_no_folder(tmp_path) -> None:
    """Never its project's folder instead: it was started elsewhere for its own work (DEC-123)."""
    session = SessionId.new()
    folders = SessionFolders(
        {_PROJECT: tmp_path}, _Store({}), _Folders({session: tmp_path / "gone"})
    )

    assert await folders.folder_for(session, _PROJECT) is None


@pytest.mark.asyncio
async def test_a_worktree_binding_is_asked_before_a_folder_binding(tmp_path) -> None:
    session = SessionId.new()
    worktree = tmp_path / "worktrees" / "one"
    worktree.mkdir(parents=True)
    there = tmp_path / "there"
    there.mkdir()
    folders = SessionFolders(
        {_PROJECT: tmp_path}, _Store({session: _worktree(worktree)}), _Folders({session: there})
    )

    assert await folders.folder_for(session, _PROJECT) == worktree
