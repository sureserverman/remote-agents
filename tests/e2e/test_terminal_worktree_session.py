"""A worktree session end to end on a real tmux server and a real git repository (BL-116).

One session launches regular in the main checkout; a second launches in worktree mode, and its
pane starts in its own worktree on its own `ra/…` branch while the main checkout is left as it
was. Resuming a conversation found in that worktree lands in the same worktree, and rolling the
worktree session over starts its successor there too. The services are the production ones
over the real SQLite stores, the real `GitCli` and the real `TmuxTerminal`; only the agent is a
harmless fake (the rollover e2e's). Run alone: real-tmux tests fail spuriously beside another
uv/pytest.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from test_terminal_rollover import _AGENT, _COMPOSER, HANDOFF, _type

from remote_agents.adapters.git.checkout import GitCli
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.application.commands import LaunchCommand, ResumeCommand
from remote_agents.application.services import SessionService
from remote_agents.application.session_folders import SessionFolders
from remote_agents.application.worktrees import WorktreeService
from remote_agents.composition.service import build_rollover_pass
from remote_agents.domain.conversations import (
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionState
from remote_agents.domain.rollover import RolloverState
from remote_agents.ports.provider_descriptor import ProviderDescriptor
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE

PROJECT = ProjectId("opaque-editor")
PROFILE = ProfileId("fake")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _host_git_config_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _cwd(intents: Path, session_id: SessionId) -> str:
    return json.loads((intents / f"{session_id}.json").read_text(encoding="utf-8"))["cwd"]


async def test_a_worktree_session_launches_resumes_and_rolls_over_in_its_worktree(
    tmp_path: Path,
) -> None:
    project = tmp_path / "opaque-editor"
    project.mkdir()
    _git(project, "init", "-q", "--template=", "-b", "main")
    _git(project, "config", "user.email", "t@example.invalid")
    _git(project, "config", "user.name", "t")
    (project / ".gitignore").write_text(".claude/\nCLAUDE.md\n", encoding="utf-8")
    (project / "f.txt").write_text("one\n", encoding="utf-8")
    _git(project, "add", ".gitignore", "f.txt")
    _git(project, "commit", "-q", "-m", "base")
    (project / "CLAUDE.md").write_text("instructions\n", encoding="utf-8")
    main_before = _git(project, "status", "--porcelain", "--ignored")
    head = _git(project, "rev-parse", "HEAD")

    agent, mode_file = tmp_path / "fake_agent.py", tmp_path / "mode"
    agent.write_text(_AGENT, encoding="utf-8")
    mode_file.write_text("accept", encoding="utf-8")
    intents = tmp_path / "i"
    connection = open_database(tmp_path / "sessions.sqlite3")
    store = SQLiteSessionStore(connection)
    worktrees = SQLiteWorktreeStore(connection)
    folders = SessionFolders({PROJECT: project}, worktrees)
    socket = f"remote-agents-test-{uuid4().hex}"
    gateway = TmuxGateway(socket, AsyncTmuxRunner(), intent_directory=intents)

    def profile(session_id: SessionId, *_ignored: object) -> LaunchProfile:
        return LaunchProfile(
            sys.executable,
            (sys.executable, str(agent), HANDOFF, str(mode_file)),
            {"PATH": os.environ["PATH"], SESSION_ID_VARIABLE: str(session_id)},
            "READY",
        )

    terminal = TmuxTerminal(
        gateway,
        {PROJECT: project},
        {},
        startup_timeout=10.0,
        profile_factories={PROFILE: profile},
        resume_profile_factories={PROFILE: profile},
        composers={"fake": ProviderDescriptor(PROFILE, "F", "Fake", composer=_COMPOSER)},
        folder_for=folders.folder_for,
    )
    sessions = SessionService(
        store,
        terminal,
        worktrees=worktrees,
        worktree_service=WorktreeService(
            git=GitCli(),
            store=worktrees,
            sessions=store,
            project_paths={PROJECT: project},
            directory=tmp_path / "state" / "worktrees",
            copy_for=lambda _path: ("CLAUDE.md",),
        ),
    )

    async def on() -> bool:
        return True

    rollover_pass = build_rollover_pass(
        connection=connection,
        sessions=sessions,
        terminal=terminal,
        enabled=on,
        project_paths={PROJECT: project},
        rollable=frozenset({PROFILE}),
        worktrees=worktrees,
    )
    rollovers = SQLiteRolloverStore(connection)
    launched: list[SessionId] = []
    try:
        # 1. A regular session, in the main checkout.
        assert not await sessions.worktree_offered(PROJECT)
        regular = (await sessions.launch(LaunchCommand(PROJECT, PROFILE, "regular"))).record
        launched.append(regular.session_id)
        assert regular.state is SessionState.RUNNING
        assert _cwd(intents, regular.session_id) == str(project.resolve())

        # 2. Contention: the mode is offered, and a worktree session launches in its own.
        assert await sessions.worktree_offered(PROJECT)
        outcome = await sessions.launch(LaunchCommand(PROJECT, PROFILE, "own", mode="worktree"))
        own = outcome.record
        launched.append(own.session_id)
        assert own.state is SessionState.RUNNING, outcome
        bound = await worktrees.folder_for(own.session_id)
        assert bound is not None

        # 3. Its pane starts in the worktree, on its own branch at main's HEAD, with the
        # untracked instructions copied in -- and the main checkout is untouched.
        assert _cwd(intents, own.session_id) == str(bound.path.resolve())
        assert (
            _git(bound.path, "rev-parse", "--abbrev-ref", "HEAD") == f"ra/{str(own.session_id)[:8]}"
        )
        assert _git(bound.path, "rev-parse", "HEAD") == head
        assert (bound.path / "CLAUDE.md").read_text(encoding="utf-8") == "instructions\n"
        assert _git(project, "status", "--porcelain", "--ignored") == main_before
        assert _git(project, "rev-parse", "--abbrev-ref", "HEAD") == "main"

        # 4. A conversation found in that worktree resumes into the same worktree.
        conversation = ResolvedConversation(
            ConversationSummary(
                ConversationReference(f"c-{'1':0>64}"),
                PROFILE,
                PROJECT,
                ConversationState.RESUMABLE,
                datetime(2026, 10, 10, tzinfo=UTC),
                None,
            ),
            ProviderConversationId("conversation-in-worktree"),
            folder=bound.path,
        )
        resumed = (
            await sessions.resume(ResumeCommand(PROJECT, PROFILE, conversation, "resume"))
        ).record
        launched.append(resumed.session_id)
        rebound = await worktrees.folder_for(resumed.session_id)
        assert rebound is not None and rebound.path == bound.path
        assert _cwd(intents, resumed.session_id) == str(bound.path.resolve())

        # 5. Rolled over, the worktree session's successor lands in the same worktree.
        await _type(terminal, own.session_id, "handoff")
        row = None
        for _ in range(60):
            await rollover_pass.run_once()
            rows = rollovers._select("1 = 1", ())
            row = rows[0] if rows else None
            if row is not None and row.successor_session_id is not None:
                if row.successor_session_id not in launched:
                    launched.append(row.successor_session_id)
            if row is not None and row.state is RolloverState.COMPLETED:
                break
            await asyncio.sleep(0.2)
        assert row is not None and row.state is RolloverState.COMPLETED, (
            None if row is None else (row.state, row.failure_code, row.failure_detail)
        )
        assert row.predecessor_session_id == own.session_id
        successor = row.successor_session_id
        assert successor is not None
        carried = await worktrees.folder_for(successor)
        assert carried is not None and carried.path == bound.path
        assert _cwd(intents, successor) == str(bound.path.resolve())
        assert _git(project, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        assert not (project / ".claude").exists(), "the handoff was written in the worktree"
    finally:
        for session_id in launched:
            try:
                await gateway.destroy(session_id)
            except RuntimeError:
                pass
        killing = await asyncio.create_subprocess_exec(
            "tmux", "-L", socket, "kill-server", stderr=asyncio.subprocess.DEVNULL
        )
        await killing.wait()
        connection.close()
