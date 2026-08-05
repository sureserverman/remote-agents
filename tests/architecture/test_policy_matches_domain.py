"""The action policy may never offer what the domain refuses to transition.

Stage 1 shipped `force` from ORPHANED on the reasoning that the state could be stopped from
neither surface. It could not: ORPHANED is read-only in the state machine, so the offered
button raised InvalidTransition before the terminal was ever reached. Offering an action the
domain will reject is strictly worse than offering nothing — the owner confirms a destructive
operation and gets an exception instead of a stopped session.

This test is the structural guard that makes that class of mistake impossible to reintroduce:
availability is a *presentation* narrowing of the domain's legal transitions, never a widening.
"""

from __future__ import annotations

import pytest

from remote_agents.application.session_actions import CLEANUP, FORCE, GRACEFUL, available_actions
from remote_agents.domain.models import SessionState
from remote_agents.domain.state_machine import InvalidTransition, LifecycleEvent, transition

# The event each offerable action ultimately asks the domain to perform, mirroring
# SessionService.graceful_stop / cleanup / force_stop.
_ACTION_EVENTS = {
    GRACEFUL: LifecycleEvent.GRACEFUL_STOP_REQUESTED,
    CLEANUP: LifecycleEvent.CLEANUP_CONFIRMED,
    FORCE: LifecycleEvent.VERIFIED_FORCE_STOP,
}


def _is_legal(state: SessionState, event: LifecycleEvent) -> bool:
    try:
        transition(state, event)
    except InvalidTransition:
        return False
    return True


@pytest.mark.parametrize("state", list(SessionState))
def test_every_offered_action_is_a_legal_domain_transition(state: SessionState) -> None:
    """No surface may offer a stop the state machine would reject."""
    for action in available_actions(state):
        event = _ACTION_EVENTS[action]
        assert _is_legal(state, event), (
            f"available_actions({state.value}) offers {action!r}, but "
            f"{event.value} is not a legal transition from {state.value} — "
            "confirming it would raise InvalidTransition instead of stopping the session"
        )


def test_every_action_maps_to_an_event() -> None:
    """A new action added to the policy must declare the event it performs."""
    offered = {action for state in SessionState for action in available_actions(state)}
    assert offered <= set(_ACTION_EVENTS)


def test_the_policy_is_a_subset_not_a_restatement_of_the_domain() -> None:
    """Availability may be narrower than the domain; it may never be broader.

    Narrower is intentional and load-bearing: cleanup is domain-legal from RUNNING and
    STOP_REQUESTED, but is deliberately offered only from PRESERVED.
    """
    narrower = [
        (state.value, action)
        for state in SessionState
        for action, event in _ACTION_EVENTS.items()
        if _is_legal(state, event) and action not in available_actions(state)
    ]
    assert narrower, "if the policy exactly restated the domain it would not be a policy"
