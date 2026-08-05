"""The one authority over which lifecycle actions a session offers."""

from __future__ import annotations

import pytest

from remote_agents.application.session_actions import available_actions
from remote_agents.domain.models import SessionState

# Enumerated reflectively so a state added to the enum later fails here until it is
# classified, rather than silently inheriting whatever the last branch returned.
EXPECTED: dict[SessionState, tuple[str, ...]] = {
    SessionState.STARTING: (),
    SessionState.RUNNING: ("graceful", "force"),
    SessionState.STOP_REQUESTED: ("force",),
    SessionState.PRESERVED: ("cleanup", "force"),
    SessionState.FAILED: ("force",),
    SessionState.ENDED: (),
    # Not forceable: the domain has no transition out of ORPHANED, so offering force here
    # would raise InvalidTransition rather than stop anything. See
    # tests/architecture/test_policy_matches_domain.py.
    SessionState.ORPHANED: (),
}


def test_every_state_is_classified() -> None:
    assert set(EXPECTED) == set(SessionState)


@pytest.mark.parametrize("state", list(SessionState))
def test_available_actions_for_every_state(state: SessionState) -> None:
    assert available_actions(state) == EXPECTED[state]


@pytest.mark.parametrize("state", list(SessionState))
def test_only_known_actions_are_ever_offered(state: SessionState) -> None:
    assert set(available_actions(state)) <= {"graceful", "cleanup", "force"}


@pytest.mark.parametrize("state", list(SessionState))
def test_graceful_only_from_running(state: SessionState) -> None:
    assert ("graceful" in available_actions(state)) is (state is SessionState.RUNNING)


@pytest.mark.parametrize("state", list(SessionState))
def test_cleanup_only_from_preserved(state: SessionState) -> None:
    assert ("cleanup" in available_actions(state)) is (state is SessionState.PRESERVED)


@pytest.mark.parametrize("state", list(SessionState))
def test_force_reconciles_the_two_prior_copies(state: SessionState) -> None:
    """The token issuer's set wins over the list builder's force-from-everything.

    ORPHANED stays out. It is the one state the two copies never agreed on, and the domain
    settles it: no event is legal from ORPHANED, so a force there raises rather than stops.
    """
    forceable = {
        SessionState.RUNNING,
        SessionState.STOP_REQUESTED,
        SessionState.PRESERVED,
        SessionState.FAILED,
    }
    assert ("force" in available_actions(state)) is (state in forceable)


def test_a_starting_session_offers_nothing() -> None:
    assert available_actions(SessionState.STARTING) == ()


def test_an_ended_session_offers_nothing() -> None:
    assert available_actions(SessionState.ENDED) == ()


def test_ordering_is_stable_and_puts_force_last() -> None:
    """Force is the destructive option; it never leads a menu."""
    for state in SessionState:
        actions = available_actions(state)
        if "force" in actions:
            assert actions[-1] == "force"
