"""Safe handoff refuses a live source, then starts one new pane on an isolated server."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.application.commands import AdoptionCommand
from remote_agents.application.errors import ExternalSessionStillRunningError
from remote_agents.application.services import SessionService
from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.external_sessions import (
    ExternalSessionReference,
    ExternalSessionState,
    ExternalSessionSummary,
    ResolvedExternalSession,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionId


class ProcessEvidence:
    def __init__(self, external: ResolvedExternalSession) -> None:
        self.external = external
        self.running = True

    async def list_external_sessions(self):
        return (self.external.summary,)

    async def resolve_external_session(self, _reference):
        return self.external

    async def is_still_running(self, _reference):
        return self.running


async def test_external_handoff_refuses_live_then_uses_only_the_isolated_tmux_socket(
    tmp_path: Path,
) -> None:
    project_id = ProjectId("opaque-editor")
    source = ProviderConversationId("source-123")
    external = ResolvedExternalSession(
        ExternalSessionSummary(
            ExternalSessionReference("p-0123456789abcdef"),
            ProfileId("claude"),
            project_id,
            ExternalSessionState.RUNNING_EXTERNALLY,
        ),
        42,
        source,
    )
    agent = tmp_path / "agent.py"
    agent.write_text("import time\nprint('READY', flush=True)\ntime.sleep(10)\n", encoding="utf-8")
    gateway = TmuxGateway(
        f"remote-agents-test-{uuid4().hex}",
        AsyncTmuxRunner(),
        intent_directory=tmp_path / "intents",
    )

    def resume_profile(_session: SessionId, received: ProviderConversationId) -> LaunchProfile:
        return LaunchProfile(
            sys.executable,
            (sys.executable, str(agent), "--resume", received.value),
            {"PATH": os.environ["PATH"]},
            "READY",
        )

    terminal = TmuxTerminal(
        gateway,
        {project_id: tmp_path},
        {},
        startup_timeout=1,
        resume_profile_factories={ProfileId("claude"): resume_profile},
    )
    evidence = ProcessEvidence(external)
    service = SessionService(
        SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3")),
        terminal,
        processes=evidence,
    )
    try:
        with pytest.raises(ExternalSessionStillRunningError):
            await service.adopt(AdoptionCommand(external, "adopt"))
        evidence.running = False
        adopted = await service.adopt(AdoptionCommand(external, "adopt"))
        assert (await terminal.inspect(adopted.session_id)).live
        assert len(await terminal.managed_observations()) == 1
    finally:
        for record in await service.list_sessions():
            try:
                await gateway.mutate("kill-session", f"ra-{record.session_id}")
            except RuntimeError:
                pass
