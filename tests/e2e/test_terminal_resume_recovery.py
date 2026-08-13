"""Resumed sessions retain an exact persisted intent across terminal reconstruction."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from test_terminal_launch import STARTUP_BUDGET

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.application.reconcile import ReconciliationService
from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


async def test_restarted_terminal_recovers_a_resumed_session_from_exact_tmux_ownership(
    tmp_path: Path,
) -> None:
    agent = tmp_path / "fake_agent.py"
    agent.write_text("import time\nprint('READY', flush=True)\ntime.sleep(1)\n", encoding="utf-8")
    source_id = ProviderConversationId("provider-opaque-id")
    project_id = ProjectId("opaque-editor")
    profile_id = ProfileId("claude")
    session_id = SessionId.new()
    gateway = TmuxGateway(
        f"remote-agents-test-{uuid4().hex}",
        AsyncTmuxRunner(),
        intent_directory=tmp_path / "intents",
    )

    def resume_profile(
        _session_id: SessionId, received_source: ProviderConversationId
    ) -> LaunchProfile:
        return LaunchProfile(
            sys.executable,
            (sys.executable, str(agent), "--resume", received_source.value),
            {"PATH": os.environ["PATH"]},
            "READY",
        )

    terminal = TmuxTerminal(
        gateway,
        {project_id: tmp_path},
        {},
        startup_timeout=STARTUP_BUDGET,
        resume_profile_factories={profile_id: resume_profile},
    )
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    try:
        await store.save(
            SessionRecord(
                session_id,
                project_id,
                profile_id,
                SessionDisplayIdentity("opaque-editor", "claude", "resumed", 1),
                SessionState.STARTING,
                datetime.now(UTC),
                profile_id,
                source_id.value,
            )
        )
        assert (await terminal.resume(session_id, project_id, profile_id, source_id)).live
        intent = (tmp_path / "intents" / f"{session_id}.json").read_text(encoding="utf-8")
        assert '"--resume", "provider-opaque-id"' in intent

        restarted = TmuxTerminal(
            gateway, {project_id: tmp_path}, {}, startup_timeout=STARTUP_BUDGET
        )
        recovered = await ReconciliationService(store, settle_after=timedelta(0)).reconcile(
            await restarted.managed_observations()
        )

        assert recovered[0].state is SessionState.RUNNING
        assert (await store.get(session_id)).state is SessionState.RUNNING
    finally:
        try:
            await gateway.mutate("kill-session", f"ra-{session_id}")
        except RuntimeError:
            pass
