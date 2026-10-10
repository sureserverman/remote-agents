"""Each resume list sees its project's live worktree folders too (BL-116).

A session that ran in a worktree leaves its conversation filed under that folder, not the
project's, so a catalogue that listed the project folder alone would lose every worktree
conversation from the resume list. Each one found there carries the folder, so resuming it
lands back in the same worktree. A removed worktree is not listed: its folder is gone.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.agents.claude.sessions import ClaudeSessionCatalogue
from remote_agents.adapters.agents.codex.sessions import CodexSessionCatalogue
from remote_agents.adapters.agents.opencode.sessions import OpenCodeSessionCatalogue
from remote_agents.domain.models import ProjectId

_PROJECT = ProjectId("opaque-editor")
_UPDATED = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


class _Folders:
    """The live worktree folders per project; a removed one is simply not here."""

    def __init__(self, live: dict[ProjectId, tuple[Path, ...]]) -> None:
        self._live = live
        self.asked: list[ProjectId] = []

    async def __call__(self, project_id: ProjectId) -> tuple[Path, ...]:
        self.asked.append(project_id)
        return self._live.get(project_id, ())


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    live = tmp_path / "worktrees" / "live"
    removed = tmp_path / "worktrees" / "removed"
    for folder in (project, live, removed):
        folder.mkdir(parents=True)
    return project, live, removed


async def _listed(catalogue) -> dict[str, Path | None]:
    page = await catalogue.list_conversations(
        profile_id=None, project_id=_PROJECT, page=1, page_size=50
    )
    assert page.unavailable_reason is None, page.unavailable_reason
    resolved = [await catalogue.resolve_conversation(item.reference) for item in page.conversations]
    return {entry.provider_conversation_id.value: entry.folder for entry in resolved if entry}


@pytest.mark.asyncio
async def test_claude_lists_worktree_conversations_tagged_with_their_folder(tmp_path) -> None:
    project, live, removed = _layout(tmp_path)
    root = tmp_path / "claude-projects"
    ids = {
        project: "00000000-0000-4000-8000-000000000001",
        live: "00000000-0000-4000-8000-000000000002",
        removed: "00000000-0000-4000-8000-000000000003",
    }
    for folder, conversation in ids.items():
        # Claude's rule as measured (every non-alphanumeric as "-"), not the code's spelling.
        name = "".join(c if c.isascii() and c.isalnum() else "-" for c in str(folder.resolve()))
        directory = root / name
        directory.mkdir(parents=True)
        (directory / f"{conversation}.jsonl").write_text("{}\n", encoding="utf-8")
    catalogue = ClaudeSessionCatalogue(
        {_PROJECT: project}, sessions_root=root, worktree_folders=_Folders({_PROJECT: (live,)})
    )

    listed = await _listed(catalogue)

    assert listed == {ids[project]: None, ids[live]: live}


class _Threads:
    def __init__(self, by_folder: Mapping[Path, str]) -> None:
        self._by_folder = by_folder

    async def list_threads(self, *, cwd: Path, cursor: str | None, limit: int):
        conversation = self._by_folder.get(cwd)
        rows = [] if conversation is None else [{"id": conversation, "updatedAt": 1760086800}]
        return {"data": rows, "nextCursor": None}


@pytest.mark.asyncio
async def test_codex_lists_worktree_conversations_tagged_with_their_folder(tmp_path) -> None:
    project, live, removed = _layout(tmp_path)
    threads = _Threads({project: "thread-main", live: "thread-live", removed: "thread-gone"})
    catalogue = CodexSessionCatalogue(
        {_PROJECT: project}, threads, worktree_folders=_Folders({_PROJECT: (live,)})
    )

    listed = await _listed(catalogue)

    assert listed == {"thread-main": None, "thread-live": live}


class _Runner:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    async def list_sessions(self, limit: int) -> str:
        return json.dumps(self._rows)


@pytest.mark.asyncio
async def test_opencode_lists_worktree_conversations_tagged_with_their_folder(tmp_path) -> None:
    project, live, removed = _layout(tmp_path)
    stamp = _UPDATED.isoformat()
    runner = _Runner(
        [
            {"id": "ses_main", "directory": str(project), "updatedAt": stamp},
            {"id": "ses_live", "directory": str(live), "updatedAt": stamp},
            {"id": "ses_gone", "directory": str(removed), "updatedAt": stamp},
        ]
    )
    catalogue = OpenCodeSessionCatalogue(
        {_PROJECT: project}, runner, worktree_folders=_Folders({_PROJECT: (live,)})
    )

    listed = await _listed(catalogue)

    assert listed == {"ses_main": None, "ses_live": live}


@pytest.mark.asyncio
async def test_a_catalogue_with_no_worktree_lookup_lists_the_project_folder_alone(
    tmp_path,
) -> None:
    project, live, _removed = _layout(tmp_path)
    threads = _Threads({project: "thread-main", live: "thread-live"})
    catalogue = CodexSessionCatalogue({_PROJECT: project}, threads)

    assert await _listed(catalogue) == {"thread-main": None}


async def _broken(_project_id: ProjectId) -> tuple[Path, ...]:
    raise sqlite3.OperationalError("database is locked")


@pytest.mark.asyncio
async def test_a_worktree_read_that_fails_still_lists_the_project_folder(tmp_path) -> None:
    """The worktree rows are another process's database: a locked read costs the worktree
    conversations this listing, never the project's own (or other providers')."""
    project, _live, _removed = _layout(tmp_path)
    root = tmp_path / "claude-projects"
    name = "".join(c if c.isascii() and c.isalnum() else "-" for c in str(project.resolve()))
    (root / name).mkdir(parents=True)
    (root / name / "00000000-0000-4000-8000-000000000001.jsonl").write_text("{}\n")
    stamp = _UPDATED.isoformat()
    catalogues = (
        ClaudeSessionCatalogue({_PROJECT: project}, sessions_root=root, worktree_folders=_broken),
        CodexSessionCatalogue(
            {_PROJECT: project}, _Threads({project: "thread-main"}), worktree_folders=_broken
        ),
        OpenCodeSessionCatalogue(
            {_PROJECT: project},
            _Runner([{"id": "ses_main", "directory": str(project), "updatedAt": stamp}]),
            worktree_folders=_broken,
        ),
    )

    for catalogue in catalogues:
        assert len(await _listed(catalogue)) == 1, type(catalogue).__name__


class _ManyThreads:
    """A busy main checkout: more threads than the catalogue keeps, all older than the
    worktree's one."""

    def __init__(self, project: Path, worktree: Path) -> None:
        self._project = project
        self._worktree = worktree
        self.asked: list[Path] = []

    async def list_threads(self, *, cwd: Path, cursor: str | None, limit: int):
        self.asked.append(cwd)
        if cwd == self._worktree:
            return {"data": [{"id": "thread-worktree", "updatedAt": 1760090000}]}
        start = int(cursor or 0)
        rows = [
            {"id": f"thread-{n}", "updatedAt": 1700000000 + n} for n in range(start, start + limit)
        ]
        return {"data": rows, "nextCursor": str(start + limit) if start + limit < 300 else None}


@pytest.mark.asyncio
async def test_a_busy_main_checkout_does_not_crowd_out_a_worktrees_newest_thread(
    tmp_path,
) -> None:
    project, live, _removed = _layout(tmp_path)
    threads = _ManyThreads(project, live)
    catalogue = CodexSessionCatalogue(
        {_PROJECT: project}, threads, worktree_folders=_Folders({_PROJECT: (live,)})
    )

    listed = await _listed(catalogue)

    assert listed.get("thread-worktree") == live


@pytest.mark.asyncio
async def test_a_worktree_folder_is_asked_for_by_its_resolved_path(tmp_path) -> None:
    """The pane starts in the resolved folder, so that is the cwd the agent records."""
    project, live, _removed = _layout(tmp_path)
    alias = tmp_path / "state-link"
    alias.symlink_to(live.parent, target_is_directory=True)
    threads = _Threads({project: "thread-main", live.resolve(): "thread-live"})
    catalogue = CodexSessionCatalogue(
        {_PROJECT: project}, threads, worktree_folders=_Folders({_PROJECT: (alias / "live",)})
    )

    assert "thread-live" in await _listed(catalogue)
