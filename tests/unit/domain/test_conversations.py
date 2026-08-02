from datetime import UTC, datetime

import pytest

from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProviderConversationId,
    display_description,
)
from remote_agents.domain.models import ProfileId, ProjectId


def test_conversation_summary_exposes_only_opaque_reference_and_safe_metadata() -> None:
    summary = ConversationSummary(
        ConversationReference("c-0123456789abcdef"),
        ProfileId("claude"),
        ProjectId("remote-agents"),
        ConversationState.RESUMABLE,
        datetime.now(UTC),
    )

    assert str(summary.reference) == "c-0123456789abcdef"
    assert "provider_conversation_id" not in ConversationSummary.__dataclass_fields__


@pytest.mark.parametrize("value", ("", "c-short", "c-INVALID_token", "c-" + "a" * 65))
def test_conversation_reference_rejects_non_opaque_values(value: str) -> None:
    with pytest.raises(ValueError, match="bounded opaque"):
        ConversationReference(value)


def test_provider_conversation_id_is_bounded_and_cannot_contain_whitespace() -> None:
    assert ProviderConversationId("thread-123").value == "thread-123"
    with pytest.raises(ValueError, match="bounded visible"):
        ProviderConversationId("thread 123")


def test_catalogue_page_rejects_invalid_pagination() -> None:
    with pytest.raises(ValueError, match="page bounds"):
        ConversationCataloguePage((), 2, 1)


def test_display_description_normalizes_and_bounds_an_owner_approved_title() -> None:
    assert display_description("  Useful\n title ") == "Useful title"
    assert display_description("x" * 121) == "x" * 120
    assert display_description(42) is None
