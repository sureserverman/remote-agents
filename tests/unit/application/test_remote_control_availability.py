"""Remote Control availability is a lifecycle property, not a Telegram one."""

from __future__ import annotations

from uuid import UUID

import pytest

from remote_agents.application.session_actions import (
    REMOTE_CONTROL_LABELS,
    RemoteControlDirection,
    remote_control_available,
    remote_control_directions,
)
from remote_agents.domain.models import ProfileId, SessionId, SessionState
from remote_agents.domain.remote_control import RemoteControlState

RUNNING = SessionState.RUNNING


class Record:
    """The minimum a caller must carry to be asked about Remote Control."""

    # Mirrors SessionRecord's tenth field. A fake missing it duck-types the record
    # everywhere except the one branch DEC-020 added, which is the branch that offers a
    # destructive action.
    orphan_provenance = None

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


# --- Which direction, now that there is only one -----------------------------------------
#
# The pane's state used to pick the direction at render time, so the button said "Remote
# Control on" or "Remote Control off" and the observation stored on the record decided which.
# It is one button now, and the direction is resolved at *confirm* time from a fresh pane
# read -- so these tests pin that `remote_control_directions` has stopped answering "which
# way", and answers only "may this session be toggled at all".


@pytest.mark.parametrize(
    "observed",
    [None, RemoteControlState.ACTIVE, RemoteControlState.INACTIVE, RemoteControlState.UNKNOWN],
)
def test_a_running_claude_session_offers_one_direction_whatever_was_observed(
    observed: RemoteControlState | None,
) -> None:
    record = Record(SessionState.RUNNING, ProfileId("claude"))

    assert remote_control_directions(record, observed) == (RemoteControlDirection.TOGGLE,)


@pytest.mark.parametrize("state", [state for state in SessionState if state is not RUNNING])
def test_a_session_that_cannot_be_toggled_offers_no_direction(state: SessionState) -> None:
    record = Record(state, ProfileId("claude"))

    assert remote_control_directions(record, RemoteControlState.ACTIVE) == ()


@pytest.mark.parametrize("profile", ["codex", "cursor", "opencode", "aider"])
def test_no_other_profile_offers_a_direction_either(profile: str) -> None:
    record = Record(SessionState.RUNNING, ProfileId(profile))

    assert remote_control_directions(record, None) == ()


def test_the_one_direction_is_named_without_naming_a_direction() -> None:
    """The label must not say `on` or `off`: which way it goes is not known until it is read."""
    assert REMOTE_CONTROL_LABELS == {RemoteControlDirection.TOGGLE: "Remote Control"}
