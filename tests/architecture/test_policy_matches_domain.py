"""The action policy may never offer what the domain refuses to transition.

Stage 1 shipped `force` from ORPHANED on the reasoning that the state could be stopped from
neither surface. It could not: ORPHANED was read-only in the state machine, so the offered
button raised InvalidTransition before the terminal was ever reached. Offering an action the
domain will reject is strictly worse than offering nothing — the owner takes an offered action
and gets an exception instead of a stopped session.

This test is the structural guard that makes that class of mistake impossible to reintroduce:
availability is a *presentation* narrowing of the domain's legal transitions, never a widening.

**DEC-020 later gave ORPHANED exactly one way out, which narrows what this file can prove.**
The matrix is a pure function of `SessionState` and cannot read `orphan_provenance`, so it
now permits a force from *either* kind of ORPHANED and no longer refuses the muddled-evidence
one. The guard against that is `available_actions`, backed by a check in
`SessionService.force_stop`; the last test in this file pins exactly that division of labour
so a reader does not mistake the domain's permission for the policy's.
"""

from __future__ import annotations

import pytest

from remote_agents.application.session_actions import CLEANUP, FORCE, GRACEFUL, available_actions
from remote_agents.domain.models import OrphanProvenance, SessionState
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


PROVENANCES: tuple[OrphanProvenance | None, ...] = (None, *OrphanProvenance)
"""Every provenance a stored record can present, including the pre-migration-6 `None`.

Parametrizing over `SessionState` alone stopped being exhaustive when DEC-020 made the
policy a function of two things. A sweep over states only would have covered the adopted
branch — the one that offers a *destructive* action — with no case at all.
"""


@pytest.mark.parametrize("provenance", PROVENANCES)
@pytest.mark.parametrize("state", list(SessionState))
def test_every_offered_action_is_a_legal_domain_transition(
    state: SessionState, provenance: OrphanProvenance | None
) -> None:
    """No surface may offer a stop the state machine would reject."""
    for action in available_actions(state, provenance):
        event = _ACTION_EVENTS[action]
        assert _is_legal(state, event), (
            f"available_actions({state.value}, {provenance}) offers {action!r}, but "
            f"{event.value} is not a legal transition from {state.value} — "
            "confirming it would raise InvalidTransition instead of stopping the session"
        )


def test_every_action_maps_to_an_event() -> None:
    """A new action added to the policy must declare the event it performs."""
    offered = {
        action
        for state in SessionState
        for provenance in PROVENANCES
        for action in available_actions(state, provenance)
    }
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
        if _is_legal(state, event) and action not in available_actions(state, None)
    ]
    assert narrower, "if the policy exactly restated the domain it would not be a policy"


def test_the_domain_permits_the_force_the_policy_confines_to_the_adopted_branch() -> None:
    """The one place the policy narrows on something the domain cannot see.

    Everywhere else, availability narrows the domain on `SessionState`, which both layers
    know about. DEC-020's transition is legal from ORPHANED *regardless* of provenance —
    the matrix is a pure function of state and cannot read a record field — so the domain
    is not the guard for the muddled-evidence branch. `available_actions` is the only thing
    standing between a caller and a force stop on a record whose evidence supports none.

    Pinned so that a later reader who checks the matrix, sees the transition is legal, and
    concludes the refusal is redundant, has to delete a test that says why it is not.
    """
    assert _is_legal(SessionState.ORPHANED, LifecycleEvent.VERIFIED_FORCE_STOP)
    assert FORCE in available_actions(SessionState.ORPHANED, OrphanProvenance.ADOPTED)
    for conservative in (None, OrphanProvenance.AMBIGUOUS):
        assert FORCE not in available_actions(SessionState.ORPHANED, conservative)


def test_the_resume_identity_index_releases_exactly_the_domain_s_terminal_states() -> None:
    """Migration 8's predicate is a SQL string; `TERMINAL_STATES` is derived from the
    transition matrix. Nothing made them agree, and the claim that they cannot drift was only
    true if something enforced it — so this is that something.

    A new edge out of ENDED, or a new state the matrix offers no way out of, changes
    `TERMINAL_STATES` and silently leaves the index releasing the wrong set: too narrow traps
    a conversation forever, too wide lets a second pane start against one that may be live.
    """
    from remote_agents.adapters.sqlite.migrations import MIGRATIONS
    from remote_agents.domain.state_machine import TERMINAL_STATES

    predicate = next(sql for version, sql in MIGRATIONS if version == 8)
    released = {state for state in SessionState if f"<> '{state.value}'" in predicate}

    assert released == set(TERMINAL_STATES), (
        "the index releases a conversation for exactly the states the domain calls terminal"
    )
