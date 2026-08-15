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
    (SessionState.FAILED, LifecycleEvent.READY): SessionState.RUNNING,
    (SessionState.STARTING, LifecycleEvent.AMBIGUOUS_TERMINAL_EVIDENCE): SessionState.ORPHANED,
    (SessionState.RUNNING, LifecycleEvent.GRACEFUL_STOP_REQUESTED): SessionState.STOP_REQUESTED,
    (SessionState.RUNNING, LifecycleEvent.VERIFIED_FORCE_STOP): SessionState.ENDED,
    (SessionState.RUNNING, LifecycleEvent.CLEANUP_CONFIRMED): SessionState.ENDED,
    (SessionState.RUNNING, LifecycleEvent.AMBIGUOUS_TERMINAL_EVIDENCE): SessionState.ORPHANED,
    (SessionState.STOP_REQUESTED, LifecycleEvent.PANE_EXITED): SessionState.PRESERVED,
    (SessionState.STOP_REQUESTED, LifecycleEvent.GRACEFUL_STOP_TIMED_OUT): SessionState.RUNNING,
    (SessionState.STOP_REQUESTED, LifecycleEvent.GRACEFUL_STOP_NEVER_SENT): SessionState.RUNNING,
    (SessionState.STOP_REQUESTED, LifecycleEvent.VERIFIED_FORCE_STOP): SessionState.ENDED,
    (SessionState.STOP_REQUESTED, LifecycleEvent.CLEANUP_CONFIRMED): SessionState.ENDED,
    (
        SessionState.STOP_REQUESTED,
        LifecycleEvent.AMBIGUOUS_TERMINAL_EVIDENCE,
    ): SessionState.ORPHANED,
    (SessionState.PRESERVED, LifecycleEvent.VERIFIED_FORCE_STOP): SessionState.ENDED,
    (SessionState.PRESERVED, LifecycleEvent.CLEANUP_CONFIRMED): SessionState.ENDED,
    (SessionState.FAILED, LifecycleEvent.VERIFIED_FORCE_STOP): SessionState.ENDED,
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


def test_a_stop_that_was_never_sent_lands_where_a_timeout_does_and_is_still_its_own_event() -> None:
    """Same destination, different event — which is the whole of DEC-022.

    The two are indistinguishable from the record's point of view: nothing was stopped and the
    session is running again either way, so sharing a destination is correct. What was wrong
    was sharing an *event*, because the durable history then asserts a timeout for a stop that
    never left this host. The destination is asserted here beside the timeout's so that a
    later edit which "simplifies" one of them into the other has to change a line that says
    they are deliberately the same.
    """
    never_sent = transition(SessionState.STOP_REQUESTED, LifecycleEvent.GRACEFUL_STOP_NEVER_SENT)
    timed_out = transition(SessionState.STOP_REQUESTED, LifecycleEvent.GRACEFUL_STOP_TIMED_OUT)

    assert never_sent.to_state is SessionState.RUNNING
    assert never_sent.to_state is timed_out.to_state
    assert never_sent.event is not timed_out.event


def test_preserved_session_is_created_only_by_a_dead_pane_after_graceful_stop() -> None:
    result = transition(SessionState.STOP_REQUESTED, LifecycleEvent.PANE_EXITED)

    assert result.to_state is SessionState.PRESERVED


@pytest.mark.parametrize("state", [SessionState.ENDED, SessionState.ORPHANED])
def test_terminal_and_orphaned_sessions_are_read_only(state: SessionState) -> None:
    for event in LifecycleEvent:
        with pytest.raises(InvalidTransition):
            transition(state, event)
