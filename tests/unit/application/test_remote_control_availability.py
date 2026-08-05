"""Remote Control availability is a lifecycle property, not a Telegram one."""

from __future__ import annotations

from uuid import UUID

import pytest

from remote_agents.application.session_actions import remote_control_available
from remote_agents.domain.models import ProfileId, SessionId, SessionState


class Record:
    """The minimum a caller must carry to be asked about Remote Control."""

    def __init__(self, state: SessionState, profile_id: ProfileId) -> None:
        self.session_id = SessionId(UUID(int=1))
        self.state = state
        self.profile_id = profile_id


@pytest.mark.parametrize("state", list(SessionState))
def test_claude_offers_remote_control_only_while_running(state: SessionState) -> None:
    record = Record(state, ProfileId("claude"))
    assert remote_control_available(record) is (state is SessionState.RUNNING)


@pytest.mark.parametrize("state", list(SessionState))
@pytest.mark.parametrize("profile", ["codex", "cursor", "opencode", "aider"])
def test_no_other_profile_ever_offers_remote_control(state: SessionState, profile: str) -> None:
    record = Record(state, ProfileId(profile))
    assert remote_control_available(record) is False


def test_a_running_claude_session_is_the_single_positive_case() -> None:
    positives = [
        (state, profile)
        for state in SessionState
        for profile in ("claude", "codex", "cursor")
        if remote_control_available(Record(state, ProfileId(profile)))
    ]
    assert positives == [(SessionState.RUNNING, "claude")]
