"""Resume callbacks keep provider identity server-side through confirmation."""

from __future__ import annotations

from datetime import UTC, datetime

from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.application.conversations import ConversationService
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProfileResumeCapability,
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


class Catalogue:
    def __init__(self, resolved: ResolvedConversation) -> None:
        self.resolved = resolved

    async def list_conversations(self, **_kwargs) -> ConversationCataloguePage:
        return ConversationCataloguePage((self.resolved.summary,), 1, 1)

    async def resolve_conversation(self, reference: ConversationReference):
        return self.resolved if reference == self.resolved.summary.reference else None

    async def resume_capabilities(self):
        return (
            ProfileResumeCapability(ProfileId("claude"), True, True),
            ProfileResumeCapability(
                ProfileId("cursor-agent"), False, False, "structured_catalogue_unavailable"
            ),
        )


class Launcher:
    def __init__(self) -> None:
        self.commands = []

    async def list_sessions(self):
        return ()

    async def resume(self, command):
        self.commands.append(command)
        return SessionRecord(
            SessionId.new(),
            command.project_id,
            command.profile_id,
            SessionDisplayIdentity("opaque-editor", "claude", "resumed", 1),
            SessionState.RUNNING,
            datetime.now(UTC),
        )


async def test_resume_picker_is_opaque_paginated_and_requires_one_confirmation() -> None:
    project = CatalogProject("a" * 24, "opaque-editor", "writing", "Registered")
    summary = ConversationSummary(
        ConversationReference("c-0123456789abcdef"),
        ProfileId("claude"),
        ProjectId(project.opaque_id),
        ConversationState.RESUMABLE,
        datetime(2026, 8, 2, 18, 30, tzinfo=UTC),
    )
    resolved = ResolvedConversation(summary, ProviderConversationId("provider-private-id"))
    launcher = Launcher()
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=(project,),
        profiles=(ProfileAvailability("claude", True), ProfileAvailability("cursor-agent", True)),
        launcher=launcher,
        conversations=ConversationService(Catalogue(resolved)),
    )
    await boundary._home_reply()

    profiles = await boundary._resume_profiles_reply(project.opaque_id)
    catalogue = await boundary._resume_catalogue_reply(f"{project.opaque_id}|claude|1")
    selection = catalogue.keyboard[0][0].callback_data
    boundary.callbacks.bind_pending(11, 1)
    selection_state = boundary.callbacks.resolve(selection, owner_id=7, chat_id=11, message_id=1)

    assert "Cursor Agent (structured_catalogue_unavailable)" in profiles.text
    assert selection_state is not None and selection_state.action == "resume.select"
    assert "provider-private-id" not in catalogue.text + selection

    confirmation = await boundary._resume_confirm_reply(selection_state.entity_id)
    token = confirmation.keyboard[0][0].callback_data
    boundary.callbacks.bind_pending(11, 1)
    result = await boundary._resume_reply(selection_state.entity_id, token, 1)

    assert "Session resumed" in result["text"]
    assert len(launcher.commands) == 1
    assert launcher.commands[0].conversation == resolved
    replayed = await boundary._resume_reply(selection_state.entity_id, token, 1)
    assert replayed["text"] == "That action has already run."


async def test_resume_picker_renders_a_bounded_provider_title_without_its_source_id() -> None:
    project = CatalogProject("a" * 24, "opaque-editor", "writing", "Registered")
    summary = ConversationSummary(
        ConversationReference("c-0123456789abcdef"),
        ProfileId("claude"),
        ProjectId(project.opaque_id),
        ConversationState.RESUMABLE,
        datetime(2026, 8, 2, 18, 30, tzinfo=UTC),
        "A useful title that comfortably identifies this conversation",
    )
    resolved = ResolvedConversation(summary, ProviderConversationId("provider-private-id"))
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=(project,),
        profiles=(ProfileAvailability("claude", True),),
        launcher=Launcher(),
        conversations=ConversationService(Catalogue(resolved)),
    )

    await boundary._home_reply()
    catalogue = await boundary._resume_catalogue_reply(f"{project.opaque_id}|claude|1")

    assert catalogue.keyboard[0][0].text.startswith(
        "A useful title that comfortably identifies this"
    )
    assert "provider-private-id" not in catalogue.keyboard[0][0].text
