"""The fake owner journey turns a server-resolved selection into one resume command."""

from __future__ import annotations

from datetime import UTC, datetime

from backends import SessionUseCaseDouble, backend_for

from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.application.conversations import ConversationService
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.services import ResumeOutcome
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProfileResumeCapability,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionState


class FakeCatalogue:
    def __init__(self, resolved: ResolvedConversation) -> None:
        self.resolved = resolved

    async def list_conversations(self, **_kwargs) -> ConversationCataloguePage:
        return ConversationCataloguePage((self.resolved.summary,), 1, 1)

    async def resolve_conversation(self, reference: ConversationReference):
        return self.resolved if reference == self.resolved.summary.reference else None

    async def resume_capabilities(self):
        return (ProfileResumeCapability(ProfileId("claude"), True, True),)


class FakeLauncher(SessionUseCaseDouble):
    def __init__(self) -> None:
        self.commands = []

    async def list_sessions(self):
        return ()

    async def resume(self, command):
        self.commands.append(command)
        record = type(
            "Record",
            (),
            {
                "state": SessionState.RUNNING,
                "session_id": "00000000-0000-0000-0000-000000000001",
                "display": type("Display", (), {"rendered": "opaque-editor · Claude · resumed #1"})(),
            },
        )()
        # `created=True`: this double stands for a resume that really started a session, which
        # is what this journey exercises. The `created=False` half — a conversation already
        # bound, where the bot must not claim a resume — is pinned in
        # `test_navigation.py::test_resume_without_review_answers_every_state_it_can_return`.
        return ResumeOutcome(record, created=True)


async def test_owner_resume_journey_uses_a_catalogue_reference_not_provider_input() -> None:
    project = CatalogProject("a" * 24, "opaque-editor", "writing", "Registered")
    resolved = ResolvedConversation(
        ConversationSummary(
            ConversationReference("c-0123456789abcdef"),
            ProfileId("claude"),
            ProjectId(project.opaque_id),
            ConversationState.RESUMABLE,
            datetime(2026, 8, 2, tzinfo=UTC),
        ),
        ProviderConversationId("provider-id"),
    )
    launcher = FakeLauncher()
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(project,),
            sessions=launcher,
            conversations=ConversationService(FakeCatalogue(resolved)),
        ),
        profiles=(ProfileAvailability("claude", True),),
    )
    catalogue = await boundary._resume_catalogue_reply(f"{project.opaque_id}|claude|1")
    selection = catalogue.keyboard[0][0].callback_data
    boundary.callbacks.bind_pending(11, 1)
    selected = boundary.callbacks.resolve(selection, owner_id=7, chat_id=11, message_id=1)
    assert selected is not None

    await boundary._resume_reply(selected.entity_id, selection, 1)

    assert len(launcher.commands) == 1
    assert launcher.commands[0].conversation.provider_conversation_id.value == "provider-id"
