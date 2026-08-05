"""Every lifecycle state explains itself, and no explanation contradicts the state."""

from __future__ import annotations

import pytest

from remote_agents.application.session_actions import explain_state
from remote_agents.domain.models import SessionState


@pytest.mark.parametrize("state", list(SessionState))
def test_every_state_has_a_non_empty_explanation(state: SessionState) -> None:
    assert explain_state(state).strip()


def test_no_two_states_share_an_explanation() -> None:
    """A shared string means at least one state is being described as something it is not."""
    explanations = [explain_state(state) for state in SessionState]
    assert len(set(explanations)) == len(explanations)


def test_a_starting_session_is_not_described_as_inactive() -> None:
    """The bot's fallback said 'no longer active' for STARTING, which is the opposite."""
    text = explain_state(SessionState.STARTING).casefold()
    assert "no longer" not in text
    assert "starting" in text or "coming up" in text


@pytest.mark.parametrize("state", [SessionState.ENDED, SessionState.ORPHANED])
def test_states_that_offer_no_action_say_why(state: SessionState) -> None:
    assert explain_state(state).strip()


def test_the_orphaned_explanation_does_not_promise_a_stop_that_cannot_happen() -> None:
    """ORPHANED offers no action; its text must not imply the surface can retire it."""
    text = explain_state(SessionState.ORPHANED).casefold()
    assert "force" not in text
    assert "stop" not in text
