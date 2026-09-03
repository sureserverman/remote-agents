"""Owner resume journey over a real SQLite projection and isolated tmux server."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from backends import backend_for
from test_terminal_launch import STARTUP_BUDGET

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.telegram.service import build_private_bot, unmarked
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.application.conversations import ConversationService
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.services import SessionService
from remote_agents.application.session_actions import ACTION_LABELS, GRACEFUL
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProfileResumeCapability,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionState


class SingleConversationCatalogue:
    def __init__(self, resolved: ResolvedConversation) -> None:
        self._resolved = resolved

    async def list_conversations(self, **_kwargs) -> ConversationCataloguePage:
        return ConversationCataloguePage((self._resolved.summary,), 1, 1)

    async def resolve_conversation(self, reference: ConversationReference):
        return self._resolved if reference == self._resolved.summary.reference else None

    async def resume_capabilities(self):
        return (ProfileResumeCapability(ProfileId("claude"), True, True),)


async def test_integrated_resume_journey_uses_real_sqlite_and_an_isolated_tmux_socket(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "opaque-editor"
    project_path.mkdir()
    project = CatalogProject("a" * 24, "opaque-editor", "writing", "Registered")
    project_id = ProjectId(project.opaque_id)
    source = ProviderConversationId("provider-conversation-id")
    resolved = ResolvedConversation(
        ConversationSummary(
            ConversationReference("c-0123456789abcdef"),
            ProfileId("claude"),
            project_id,
            ConversationState.RESUMABLE,
            datetime(2026, 8, 2, tzinfo=UTC),
        ),
        source,
    )
    agent = tmp_path / "fake_agent.py"
    agent.write_text("import time\nprint('READY', flush=True)\ntime.sleep(30)\n", encoding="utf-8")
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
        {project_id: project_path},
        {},
        startup_timeout=STARTUP_BUDGET,
        resume_profile_factories={ProfileId("claude"): resume_profile},
    )
    service = SessionService(
        SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3")), terminal
    )
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(project,),
            sessions=service,
            conversations=ConversationService(SingleConversationCatalogue(resolved)),
            capture=terminal.capture,
        ),
        profiles=(ProfileAvailability("claude", True),),
    )
    try:
        profiles = await boundary._resume_profiles_reply(project.opaque_id)
        assert profiles.keyboard[0][0].text == "Claude"
        catalogue = await boundary._resume_catalogue_reply(f"{project.opaque_id}|claude|1")
        boundary.callbacks.bind_pending(11, 1)
        selection = catalogue.keyboard[0][0].callback_data
        selected = boundary.callbacks.resolve(selection, owner_id=7, chat_id=11, message_id=1)
        assert selected is not None
        await boundary._resume_reply(selected.entity_id, selection, 1)

        record = (await service.list_sessions())[0]
        assert record.state is SessionState.RUNNING
        assert record.resume_source_id == source.value
        inspection = await boundary._inspection_result(str(record.session_id))
        assert inspection is not None and inspection.text.startswith("READY")

        boundary.callbacks.bind_pending(11, 1)
        detail = await boundary._detail_reply(str(record.session_id), 1)
        graceful = next(
            button.callback_data
            for row in detail.keyboard
            for button in row
            if unmarked(button.text) == ACTION_LABELS[GRACEFUL]
        )
        boundary.callbacks.bind_pending(11, 1)
        outcome = await boundary._stop_reply("graceful", graceful, 1)
        assert (await service.list_sessions())[0].state is SessionState.ENDED
        # The result names the session it acted on and says what became of its output.
        assert "The session has ended" in outcome["text"]

        # The one button did the whole stop: the reopened detail has no second step left.
        boundary.callbacks.bind_pending(11, 1)
        detail = await boundary._detail_reply(str(record.session_id), 1)
        labels = [button.text for row in detail.keyboard for button in row]
        assert labels == ["‹ Back to sessions", "Sessions", "Launch", "Resume"]
    finally:
        for record in await service.list_sessions():
            try:
                await gateway.destroy(record.session_id)
            except RuntimeError:
                pass
