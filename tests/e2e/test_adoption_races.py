"""Handoff remains a no-op while the observed source is live, then resumes exactly once."""

from __future__ import annotations

import pytest

from remote_agents.adapters.tmux.fake import FakeTerminal
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
from remote_agents.domain.models import ProfileId, ProjectId


class Store:
    def __init__(self) -> None:
        self.records = []
        self.claims = set()

    async def get_by_resume_source(self, profile_id, source_id):
        return next(
            (
                item
                for item in self.records
                if item.resume_profile_id == profile_id and item.resume_source_id == source_id
            ),
            None,
        )

    async def claim_idempotency_key(self, key):
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    async def next_sequence(self, *_args):
        return len(self.records) + 1

    async def save(self, record):
        self.records.append(record)

    async def record_event(self, session_id, _event):
        return next(item for item in self.records if item.session_id == session_id)


class Processes:
    def __init__(self, external) -> None:
        self.external = external
        self.running = True

    async def list_external_sessions(self):
        return (self.external.summary,)

    async def resolve_external_session(self, _reference):
        return self.external

    async def is_still_running(self, _reference):
        return self.running


async def test_handoff_rechecks_liveness_before_resuming_the_provider_source() -> None:
    external = ResolvedExternalSession(
        ExternalSessionSummary(
            ExternalSessionReference("p-0123456789abcdef"),
            ProfileId("claude"),
            ProjectId("opaque-editor"),
            ExternalSessionState.RUNNING_EXTERNALLY,
        ),
        42,
        ProviderConversationId("source-123"),
    )
    processes = Processes(external)
    service = SessionService(Store(), FakeTerminal(), processes=processes)

    with pytest.raises(ExternalSessionStillRunningError):
        await service.adopt(AdoptionCommand(external, "adopt"))

    processes.running = False
    adopted = await service.adopt(AdoptionCommand(external, "adopt"))

    assert adopted.resume_source_id == "source-123"
