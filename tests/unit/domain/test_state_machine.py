"""Exhaustive lifecycle-matrix tests."""

import pytest

from remote_agents.domain.models import SessionState
from remote_agents.domain.state_machine import (
    InvalidTransition,
    LifecycleEvent,
    transition,
)

LEGAL_TRANSITIONS = {
    (SessionState.STARTING, LifecycleEvent.READY): SessionState.RUNNING,
    (SessionState.STARTING, LifecycleEvent.STARTUP_ERROR): SessionState.FAILED,
    (SessionState.STARTING, LifecycleEvent.AMBIGUOUS_TERMINAL_EVIDENCE): SessionState.ORPHANED,
    (SessionState.RUNNING, LifecycleEvent.GRACEFUL_STOP_REQUESTED): SessionState.STOP_REQUESTED,
    (SessionState.RUNNING, LifecycleEvent.VERIFIED_FORCE_STOP): SessionState.ENDED,
    (SessionState.RUNNING, LifecycleEvent.CLEANUP_CONFIRMED): SessionState.ENDED,
    (SessionState.RUNNING, LifecycleEvent.AMBIGUOUS_TERMINAL_EVIDENCE): SessionState.ORPHANED,
    (SessionState.STOP_REQUESTED, LifecycleEvent.PANE_EXITED): SessionState.PRESERVED,
    (SessionState.STOP_REQUESTED, LifecycleEvent.GRACEFUL_STOP_TIMED_OUT): SessionState.RUNNING,
    (SessionState.STOP_REQUESTED, LifecycleEvent.VERIFIED_FORCE_STOP): SessionState.ENDED,
    (SessionState.STOP_REQUESTED, LifecycleEvent.CLEANUP_CONFIRMED): SessionState.ENDED,
    (
        SessionState.STOP_REQUESTED,
        LifecycleEvent.AMBIGUOUS_TERMINAL_EVIDENCE,
    ): SessionState.ORPHANED,
    (SessionState.PRESERVED, LifecycleEvent.VERIFIED_FORCE_STOP): SessionState.ENDED,
    (SessionState.PRESERVED, LifecycleEvent.CLEANUP_CONFIRMED): SessionState.ENDED,
    (SessionState.RUNNING, LifecycleEvent.RECONCILED_TERMINAL_MISSING): SessionState.ENDED,
    (SessionState.STOP_REQUESTED, LifecycleEvent.RECONCILED_TERMINAL_MISSING): SessionState.ENDED,
    (SessionState.PRESERVED, LifecycleEvent.RECONCILED_TERMINAL_MISSING): SessionState.ENDED,
    (SessionState.RUNNING, LifecycleEvent.RECONCILED_PANE_DEAD): SessionState.PRESERVED,
    (SessionState.PRESERVED, LifecycleEvent.AMBIGUOUS_TERMINAL_EVIDENCE): SessionState.ORPHANED,
    (SessionState.FAILED, LifecycleEvent.AMBIGUOUS_TERMINAL_EVIDENCE): SessionState.ORPHANED,
}


@pytest.mark.parametrize(
    ("from_state", "event", "expected"), [(*key, value) for key, value in LEGAL_TRANSITIONS.items()]
)
def test_transition_accepts_each_architecture_transition(
    from_state: SessionState, event: LifecycleEvent, expected: SessionState
) -> None:
    result = transition(from_state, event)

    assert result.to_state is expected


@pytest.mark.parametrize(
    ("from_state", "event"),
    [
        (state, event)
        for state in SessionState
        for event in LifecycleEvent
        if (state, event) not in LEGAL_TRANSITIONS
    ],
)
def test_transition_rejects_every_unlisted_pair(
    from_state: SessionState, event: LifecycleEvent
) -> None:
    with pytest.raises(InvalidTransition):
        transition(from_state, event)


def test_graceful_stop_timeout_returns_to_running_without_force_stop() -> None:
    result = transition(SessionState.STOP_REQUESTED, LifecycleEvent.GRACEFUL_STOP_TIMED_OUT)

    assert result.to_state is SessionState.RUNNING


def test_preserved_session_is_created_only_by_a_dead_pane_after_graceful_stop() -> None:
    result = transition(SessionState.STOP_REQUESTED, LifecycleEvent.PANE_EXITED)

    assert result.to_state is SessionState.PRESERVED


@pytest.mark.parametrize("state", [SessionState.ENDED, SessionState.ORPHANED])
def test_terminal_and_orphaned_sessions_are_read_only(state: SessionState) -> None:
    for event in LifecycleEvent:
        with pytest.raises(InvalidTransition):
            transition(state, event)
