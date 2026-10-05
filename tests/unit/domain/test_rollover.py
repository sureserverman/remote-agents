"""Exhaustive rollover-matrix, recovery-table and stop-guard tests."""

from dataclasses import dataclass
from itertools import product

import pytest

from remote_agents.domain.rollover import (
    TERMINAL,
    TRANSITIONS,
    RecoveryAction,
    RecoveryFacts,
    RolloverState,
    is_legal,
    may_stop_predecessor,
    recovery_action,
)

S = RolloverState

# Written from the lifecycle, not read back from TRANSITIONS.
LEGAL_MOVES = {
    (S.REQUESTED, S.HANDOFF_READY),
    (S.REQUESTED, S.CANCELLED),
    (S.REQUESTED, S.FAILED),
    (S.HANDOFF_READY, S.SUCCESSOR_STARTING),
    (S.HANDOFF_READY, S.FAILED),
    (S.HANDOFF_READY, S.CANCELLED),
    (S.SUCCESSOR_STARTING, S.ADOPTING),
    (S.SUCCESSOR_STARTING, S.FAILED),
    (S.ADOPTING, S.SUCCESSOR_ACCEPTED),
    (S.ADOPTING, S.FAILED),
    (S.SUCCESSOR_ACCEPTED, S.PREDECESSOR_STOPPING),
    (S.SUCCESSOR_ACCEPTED, S.FAILED),
    (S.PREDECESSOR_STOPPING, S.COMPLETED),
    (S.PREDECESSOR_STOPPING, S.STOP_FAILED),
}

EXPECTED_TERMINAL = {S.COMPLETED, S.FAILED, S.STOP_FAILED, S.CANCELLED}


def test_there_are_ten_states() -> None:
    assert len(RolloverState) == 10


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    list(product(RolloverState, RolloverState)),
    ids=lambda state: state.name,
)
def test_every_pair_is_legal_exactly_when_the_lifecycle_says(
    from_state: RolloverState, to_state: RolloverState
) -> None:
    expected = (from_state, to_state) in LEGAL_MOVES
    assert is_legal(from_state, to_state) is expected
    assert (to_state in TRANSITIONS.get(from_state, frozenset())) is expected


def test_derived_terminal_set_is_the_four_end_states() -> None:
    assert TERMINAL == EXPECTED_TERMINAL


ANY_FACTS = [
    RecoveryFacts(has_successor_id=has_id, successor_alive=alive, timed_out=timed_out)
    for has_id, alive, timed_out in product([False, True], repeat=3)
]


@pytest.mark.parametrize("state", sorted(EXPECTED_TERMINAL), ids=lambda state: state.name)
@pytest.mark.parametrize("facts", ANY_FACTS)
def test_terminal_states_recover_to_nothing(state: RolloverState, facts: RecoveryFacts) -> None:
    assert recovery_action(state, facts) is RecoveryAction.NOTHING


@pytest.mark.parametrize(
    "state",
    [state for state in RolloverState if state not in EXPECTED_TERMINAL],
    ids=lambda state: state.name,
)
@pytest.mark.parametrize("facts", ANY_FACTS)
def test_every_non_terminal_state_has_a_recovery_action(
    state: RolloverState, facts: RecoveryFacts
) -> None:
    assert recovery_action(state, facts) is not RecoveryAction.NOTHING


@pytest.mark.parametrize(
    ("state", "facts", "expected"),
    [
        (S.REQUESTED, RecoveryFacts(), RecoveryAction.WAIT),
        (S.HANDOFF_READY, RecoveryFacts(), RecoveryAction.LAUNCH),
        (
            S.SUCCESSOR_STARTING,
            RecoveryFacts(has_successor_id=False),
            RecoveryAction.GIVE_UP_SUCCESSOR_UNKNOWN,
        ),
        (
            S.SUCCESSOR_STARTING,
            RecoveryFacts(has_successor_id=True, successor_alive=True),
            RecoveryAction.WAIT,
        ),
        (
            S.SUCCESSOR_STARTING,
            RecoveryFacts(has_successor_id=True, successor_alive=False),
            RecoveryAction.GIVE_UP_SUCCESSOR_FAILED,
        ),
        (S.ADOPTING, RecoveryFacts(has_successor_id=True, timed_out=False), RecoveryAction.WAIT),
        (
            S.ADOPTING,
            RecoveryFacts(has_successor_id=True, timed_out=True),
            RecoveryAction.TIME_OUT,
        ),
        (
            S.SUCCESSOR_ACCEPTED,
            RecoveryFacts(has_successor_id=True, successor_alive=True),
            RecoveryAction.STOP_PREDECESSOR,
        ),
        (
            S.SUCCESSOR_ACCEPTED,
            RecoveryFacts(has_successor_id=True, successor_alive=False),
            RecoveryAction.GIVE_UP_SUCCESSOR_FAILED,
        ),
        (S.PREDECESSOR_STOPPING, RecoveryFacts(), RecoveryAction.RECONCILE_STOP),
    ],
    ids=lambda value: value.name if isinstance(value, RolloverState) else None,
)
def test_recovery_table_rows(
    state: RolloverState, facts: RecoveryFacts, expected: RecoveryAction
) -> None:
    assert recovery_action(state, facts) is expected


@pytest.mark.parametrize("facts", ANY_FACTS)
def test_successor_starting_never_relaunches(facts: RecoveryFacts) -> None:
    assert recovery_action(S.SUCCESSOR_STARTING, facts) is not RecoveryAction.LAUNCH


@pytest.mark.parametrize("facts", ANY_FACTS)
def test_only_the_accepted_and_stopping_states_recover_by_touching_the_predecessor(
    facts: RecoveryFacts,
) -> None:
    touching = {RecoveryAction.STOP_PREDECESSOR, RecoveryAction.RECONCILE_STOP}
    for state in RolloverState:
        if recovery_action(state, facts) in touching:
            assert may_stop_predecessor(state)


@dataclass(frozen=True)
class _Row:
    state: RolloverState


@pytest.mark.parametrize("state", list(RolloverState), ids=lambda state: state.name)
def test_may_stop_predecessor_only_after_acceptance(state: RolloverState) -> None:
    expected = state in {S.SUCCESSOR_ACCEPTED, S.PREDECESSOR_STOPPING}
    assert may_stop_predecessor(state) is expected
    assert may_stop_predecessor(_Row(state)) is expected
