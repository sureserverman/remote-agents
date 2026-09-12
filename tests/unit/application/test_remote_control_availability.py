"""Remote Control availability is a lifecycle property, not a Telegram one."""

from __future__ import annotations

from uuid import UUID

import pytest

from remote_agents.application.session_actions import (
    REMOTE_CONTROL_LABELS,
    RemoteControlDirection,
    remote_control_available,
    remote_control_directions,
    remote_control_reading,
    remote_control_target,
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


# --- What a confirmation should say the pane is ------------------------------------------
#
# A pane that is connected and idle prints nothing on claude 2.1.269: the connection is
# announced only inside the status menu, and only a transition leaves a line behind. A toggle
# that resolved every UNKNOWN to *on* could therefore never reach off.


@pytest.mark.parametrize(
    "stored",
    [None, RemoteControlState.ACTIVE, RemoteControlState.INACTIVE, RemoteControlState.UNKNOWN],
)
@pytest.mark.parametrize("observed", [RemoteControlState.ACTIVE, RemoteControlState.INACTIVE])
def test_a_pane_that_says_something_is_never_overruled_by_the_record(observed, stored) -> None:
    """The fresh read is a fact about now; the stored one is as old as the last toggle."""
    assert remote_control_reading(observed, stored) is observed


@pytest.mark.parametrize(
    "stored,expected",
    [
        (RemoteControlState.ACTIVE, RemoteControlState.ACTIVE),
        (RemoteControlState.INACTIVE, RemoteControlState.INACTIVE),
        (RemoteControlState.UNKNOWN, RemoteControlState.UNKNOWN),
        (None, RemoteControlState.UNKNOWN),
    ],
)
def test_a_silent_pane_falls_back_to_what_was_last_observed(stored, expected) -> None:
    assert remote_control_reading(RemoteControlState.UNKNOWN, stored) is expected


def test_the_fallback_can_only_be_wrong_in_the_direction_the_terminal_refuses() -> None:
    """A stale ACTIVE proposes *off*, and off is what an unreadable pane already declines.

    The disable path requires Claude's status menu on screen before it sends a key, and a
    disconnected pane does not show one -- so a wrong fallback costs a refusal, never a
    keystroke into somebody's session.
    """
    stale = remote_control_reading(RemoteControlState.UNKNOWN, RemoteControlState.ACTIVE)

    assert remote_control_target(stale) is RemoteControlState.INACTIVE
