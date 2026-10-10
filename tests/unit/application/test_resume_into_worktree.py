"""A conversation found in a worktree resumes into that worktree (BL-116).

Driven through the real stores and the real terminal's intent writer, with only tmux faked:
the claim is about the folder the resumed pane starts in, and that is the `cwd` the terminal
writes into the intent document before it asks tmux for anything.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
from remote_agents.adapters.tmux.gateway import TmuxInventory
from remote_agents.adapters.tmux.runtime import LaunchProfile, TmuxTerminal
from remote_agents.application.commands import ResumeCommand
from remote_agents.application.services import SessionService
from remote_agents.application.session_folders import SessionFolders
from remote_agents.domain.conversations import (
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.worktree_store import Worktree

_PROJECT = ProjectId("opaque-editor")
_CLAUDE = ProfileId("claude")


class _Gateway:
    """No pane ever comes up; only the intent the terminal writes first is read."""

    def __init__(self, intent_directory: Path) -> None:
        self.intent_directory = intent_directory
        self.launched = 0

    async def inventory(self) -> TmuxInventory:
        return TmuxInventory((), ())

    async def capture(self, _session_id: SessionId) -> str:
        return ""

    async def launch(self, *_args: object) -> None:
        self.launched += 1


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.project = tmp_path / "project"
        self.project.mkdir()
        self.intents = tmp_path / "intents"
        connection = open_database(tmp_path / "sessions.sqlite3")
        self.sessions = SQLiteSessionStore(connection)
        self.worktrees = SQLiteWorktreeStore(connection)
        self.gateway = _Gateway(self.intents)
        folders = SessionFolders({_PROJECT: self.project}, self.worktrees)
        terminal = TmuxTerminal(
            self.gateway,  # type: ignore[arg-type]
            {_PROJECT: self.project},
            {},
            startup_timeout=0.05,
            resume_profile_factories={
                _CLAUDE: lambda _s, _c: LaunchProfile(
                    "/usr/bin/claude", ("/usr/bin/claude", "--resume", "x"), {}, None, ("C-c",)
                )
            },
            folder_for=folders.folder_for,
        )
        self.service = SessionService(self.sessions, terminal, worktrees=self.worktrees)

    async def worktree(self, path: Path, *, removed: bool = False) -> Path:
        """A worktree recorded by an earlier, ended session, as a rollover or resume finds it."""
        path.mkdir(parents=True, exist_ok=True)
        earlier = SessionRecord(
            SessionId.new(),
            _PROJECT,
            _CLAUDE,
            SessionDisplayIdentity("editor", "claude", "regular", 1),
            SessionState.ENDED,
            datetime(2026, 10, 10, tzinfo=UTC),
        )
        await self.sessions.save(earlier)
        await self.worktrees.record(
            Worktree(
                path=path,
                project_id=_PROJECT,
                branch="ra/earlier",
                base_branch="main",
                base_commit="a" * 40,
                copied={},
                created_at=datetime(2026, 10, 10, tzinfo=UTC),
                removed_at=None,
            ),
            bound_to=earlier.session_id,
        )
        if removed:
            await self.worktrees.mark_removed(path, at=datetime(2026, 10, 11, tzinfo=UTC))
        return path

    async def resume(self, folder: Path | None, key: str):
        conversation = ResolvedConversation(
            ConversationSummary(
                ConversationReference(f"c-{key:0>64}"[:66]),
                _CLAUDE,
                _PROJECT,
                ConversationState.RESUMABLE,
                datetime(2026, 10, 10, tzinfo=UTC),
                None,
            ),
            ProviderConversationId(f"conversation-{key}"),
            folder=folder,
        )
        return await self.service.resume(ResumeCommand(_PROJECT, _CLAUDE, conversation, key))

    def intent_cwd(self, session_id: SessionId) -> str:
        document = json.loads((self.intents / f"{session_id}.json").read_text(encoding="utf-8"))
        return document["cwd"]


@pytest.mark.asyncio
async def test_a_conversation_found_in_a_worktree_resumes_into_it(tmp_path) -> None:
    harness = _Harness(tmp_path)
    worktree = await harness.worktree(tmp_path / "worktrees" / "one")

    outcome = await harness.resume(worktree, "1")

    bound = await harness.worktrees.folder_for(outcome.record.session_id)
    assert bound is not None and bound.path == worktree
    assert harness.intent_cwd(outcome.record.session_id) == str(worktree.resolve())


@pytest.mark.asyncio
async def test_a_conversation_found_in_the_project_folder_resumes_there(tmp_path) -> None:
    harness = _Harness(tmp_path)

    outcome = await harness.resume(None, "2")

    assert await harness.worktrees.folder_for(outcome.record.session_id) is None
    assert harness.intent_cwd(outcome.record.session_id) == str(harness.project.resolve())


@pytest.mark.asyncio
async def test_a_conversation_whose_worktree_was_removed_is_refused(tmp_path) -> None:
    """Removed between the listing and the press: refused, never resumed in the project."""
    harness = _Harness(tmp_path)
    worktree = await harness.worktree(tmp_path / "worktrees" / "gone", removed=True)

    outcome = await harness.resume(worktree, "3")

    assert outcome.created
    assert outcome.worktree_removed
    assert outcome.record.state is SessionState.FAILED
    assert harness.gateway.launched == 0
    assert not (harness.intents / f"{outcome.record.session_id}.json").exists()


@pytest.mark.asyncio
async def test_a_worktree_conversation_with_no_store_wired_is_refused(tmp_path) -> None:
    """A service that cannot bind must not quietly resume a worktree conversation elsewhere."""
    harness = _Harness(tmp_path)
    worktree = await harness.worktree(tmp_path / "worktrees" / "one")
    harness.service = SessionService(harness.sessions, harness.service._terminal)

    outcome = await harness.resume(worktree, "4")

    assert outcome.worktree_removed
    assert harness.gateway.launched == 0


class _Readers:
    """Records the workspace each usage query is searched under."""

    def __init__(self) -> None:
        self.workspaces: list[Path] = []

    def read(self, query):
        self.workspaces.append(query.workspace)
        return None


@pytest.mark.asyncio
async def test_a_worktree_sessions_usage_is_read_under_its_worktree(tmp_path) -> None:
    """Its conversation is filed under the worktree's folder, not the project's."""
    from remote_agents.composition.backend import _usage_reader

    harness = _Harness(tmp_path)
    worktree = await harness.worktree(tmp_path / "worktrees" / "one")
    bound = await harness.resume(worktree, "5")
    regular = await harness.resume(None, "6")
    readers = _Readers()
    folders = SessionFolders({_PROJECT: harness.project}, harness.worktrees)
    read = _usage_reader(harness.sessions, folders.folder_for, readers)  # type: ignore[arg-type]

    await read(bound.record.session_id)
    await read(regular.record.session_id)

    assert readers.workspaces == [worktree, harness.project]


@pytest.mark.asyncio
async def test_a_removed_worktree_sessions_usage_is_not_read_anywhere(tmp_path) -> None:
    from remote_agents.composition.backend import _usage_reader

    harness = _Harness(tmp_path)
    worktree = await harness.worktree(tmp_path / "worktrees" / "one")
    bound = await harness.resume(worktree, "7")
    await harness.worktrees.mark_removed(worktree, at=datetime(2026, 10, 12, tzinfo=UTC))
    readers = _Readers()
    folders = SessionFolders({_PROJECT: harness.project}, harness.worktrees)
    read = _usage_reader(harness.sessions, folders.folder_for, readers)  # type: ignore[arg-type]

    assert await read(bound.record.session_id) is None
    assert readers.workspaces == []
