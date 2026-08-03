"""Application DTO tests keep raw process control out of the command surface."""

from datetime import UTC, datetime

import pytest

from remote_agents.application.commands import ExternalStopCommand
from remote_agents.domain.conversations import (
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.external_sessions import (
    ExternalProcessIdentity,
    ExternalSessionReference,
    ExternalSessionState,
    ExternalSessionSummary,
    ResolvedExternalSession,
)
from remote_agents.domain.models import ProfileId, ProjectId


def test_external_stop_command_rejects_read_only_evidence() -> None:
    profile = ProfileId("claude")
    project = ProjectId("opaque-editor")
    external = ResolvedExternalSession(
        ExternalSessionSummary(
            ExternalSessionReference("p-0123456789abcdef"),
            profile,
            project,
            ExternalSessionState.NOT_SAFELY_ADOPTABLE,
        ),
        42,
        None,
        ExternalProcessIdentity(42, 9, 1000, "claude"),
    )
    conversation = ResolvedConversation(
        ConversationSummary(
            ConversationReference("c-0123456789abcdef"),
            profile,
            project,
            ConversationState.RESUMABLE,
            datetime.now(UTC),
        ),
        ProviderConversationId("source-123"),
    )

    with pytest.raises(ValueError, match="read-only"):
        ExternalStopCommand(external, conversation, "stop-1")
