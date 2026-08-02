from pathlib import Path

import pytest

from remote_agents.adapters.tmux.profiles import build_resume_profile
from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import ProfileId, SessionId
from remote_agents.domain.profiles import ProfileError, closed_profiles


@pytest.mark.parametrize(
    ("profile_id", "expected"),
    (
        ("claude", ("--resume", "source-123")),
        ("codex", ("resume", "source-123")),
        ("opencode", ("--session", "source-123")),
    ),
)
def test_resume_profile_uses_only_closed_provider_argv(profile_id, expected) -> None:
    definition = next(
        profile for profile in closed_profiles() if str(profile.profile_id) == profile_id
    )
    profile = build_resume_profile(
        definition,
        Path(f"/tools/{profile_id}"),
        SessionId.new(),
        ProviderConversationId("source-123"),
        {"PATH": "/tools"},
    )
    assert profile.argv == (f"/tools/{profile_id}", *expected)


def test_cursor_cannot_construct_a_selected_resume_argv() -> None:
    definition = next(
        profile for profile in closed_profiles() if profile.profile_id == ProfileId("cursor-agent")
    )
    with pytest.raises(ProfileError, match="no qualified"):
        build_resume_profile(
            definition,
            Path("/tools/cursor-agent"),
            SessionId.new(),
            ProviderConversationId("source-123"),
            {"PATH": "/tools"},
        )
