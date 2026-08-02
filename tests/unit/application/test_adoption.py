"""Safe handoff creates one managed resume only after local process exit evidence."""

from __future__ import annotations

import asyncio

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
from remote_agents.domain.models import ProfileId, ProjectId, SessionState


class Store:
    def __init__(self) -> None:
        self.records = []
        self.claims = set()

    async def get_by_resume_source(self, profile_id, source_id):
        return next(
            (
                record
                for record in self.records
                if record.resume_profile_id == profile_id and record.resume_source_id == source_id
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
        return next(record for record in self.records if record.session_id == session_id)


class Processes:
    def __init__(self, external: ResolvedExternalSession) -> None:
        self.external = external
        self.running = True

    async def list_external_sessions(self):
        return (self.external.summary,)

    async def resolve_external_session(self, _reference):
        return self.external

    async def is_still_running(self, _reference):
        return self.running


def external() -> ResolvedExternalSession:
    return ResolvedExternalSession(
        ExternalSessionSummary(
            ExternalSessionReference("p-0123456789abcdef"),
            ProfileId("claude"),
            ProjectId("opaque-editor"),
            ExternalSessionState.RUNNING_EXTERNALLY,
        ),
        42,
        ProviderConversationId("source-123"),
    )


async def test_adoption_refuses_a_live_external_source_without_claiming_the_callback() -> None:
    store = Store()
    processes = Processes(external())
    service = SessionService(store, FakeTerminal(), processes=processes)
    command = AdoptionCommand(processes.external, "adopt-1")

    with pytest.raises(ExternalSessionStillRunningError):
        await service.adopt(command)

    processes.running = False
    adopted = await service.adopt(command)
    assert adopted.resume_source_id == "source-123"
    assert store.claims == {"adopt-1"}


async def test_concurrent_safe_handoff_after_exit_creates_one_managed_identity() -> None:
    store = Store()
    processes = Processes(external())
    processes.running = False
    service = SessionService(store, FakeTerminal(), processes=processes)

    first, second = await asyncio.gather(
        service.adopt(AdoptionCommand(processes.external, "adopt-1")),
        service.adopt(AdoptionCommand(processes.external, "adopt-2")),
    )

    assert first.session_id == second.session_id
    assert len(store.records) == 1
    assert store.records[0].state is SessionState.STARTING
