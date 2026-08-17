from datetime import UTC, datetime

import pytest

from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.application.commands import ResumeCommand
from remote_agents.application.services import SessionService
from remote_agents.domain.conversations import (
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, ProjectId


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
        return await self.get(session_id)

    async def get(self, session_id):
        return next((record for record in self.records if record.session_id == session_id), None)


def conversation(profile_id: ProfileId = ProfileId("claude")) -> ResolvedConversation:
    summary = ConversationSummary(
        ConversationReference("c-0123456789abcdef"),
        profile_id,
        ProjectId("opaque-editor"),
        ConversationState.RESUMABLE,
        datetime.now(UTC),
    )
    return ResolvedConversation(summary, ProviderConversationId("source-123"))


async def test_resume_is_idempotent_by_provider_source_identity() -> None:
    service = SessionService(Store(), FakeTerminal())
    command = ResumeCommand(
        ProjectId("opaque-editor"), ProfileId("claude"), conversation(), "resume-1"
    )

    first = await service.resume(command)
    second = await service.resume(
        ResumeCommand(command.project_id, command.profile_id, command.conversation, "resume-2")
    )

    assert first.record.session_id == second.record.session_id
    assert first.record.resume_source_id == "source-123"
    assert first.record.display.mode == "resumed"
    # The idempotency the assertions above pin was always true and always invisible to the
    # caller: both calls answer with the same record, so a surface could not tell the one that
    # created it from the one that merely found it. That is what let the bot print "Session
    # resumed" over a session it had not started. The bit is asserted here, at the only place
    # both halves of the pair exist at once.
    assert first.created is True
    assert second.created is False


def test_resume_rejects_a_resolved_conversation_for_another_profile() -> None:
    with pytest.raises(ValueError, match="resume profile"):
        ResumeCommand(
            ProjectId("opaque-editor"),
            ProfileId("claude"),
            conversation(ProfileId("codex")),
            "key",
        )
