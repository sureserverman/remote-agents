"""Exhaustive lifecycle-matrix tests."""

import pytest

from remote_agents.domain.models import SessionState
from remote_agents.domain.state_machine import (
    TERMINAL_STATES,
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
    (SessionState.ORPHANED, LifecycleEvent.VERIFIED_FORCE_STOP): SessionState.ENDED,
    (SessionState.STARTING, LifecycleEvent.TRUST_REQUIRED): SessionState.UNTRUSTED,
    (SessionState.RUNNING, LifecycleEvent.TRUST_REQUIRED): SessionState.UNTRUSTED,
    (SessionState.FAILED, LifecycleEvent.TRUST_REQUIRED): SessionState.UNTRUSTED,
    (SessionState.UNTRUSTED, LifecycleEvent.READY): SessionState.RUNNING,
    (SessionState.UNTRUSTED, LifecycleEvent.STARTUP_ERROR): SessionState.FAILED,
    (SessionState.UNTRUSTED, LifecycleEvent.TRUST_DECLINED): SessionState.ENDED,
    (SessionState.UNTRUSTED, LifecycleEvent.VERIFIED_FORCE_STOP): SessionState.ENDED,
    (SessionState.UNTRUSTED, LifecycleEvent.AMBIGUOUS_TERMINAL_EVIDENCE): SessionState.ORPHANED,
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


def test_an_ended_session_is_read_only() -> None:
    """ORPHANED used to be asserted here beside ENDED, and is deliberately gone.

    DEC-020 gives ORPHANED exactly one way out, so a parametrization still covering it would
    assert the opposite of the decision — and would be the way this task could pass while
    proving the decision was never implemented. What replaces it is the test below, which
    pins the *shape* of the exception rather than dropping the guarantee.
    """
    for event in LifecycleEvent:
        with pytest.raises(InvalidTransition):
            transition(SessionState.ENDED, event)


def test_orphaned_offers_exactly_one_way_out_and_it_is_not_a_retire() -> None:
    """DEC-020's "exactly one outgoing transition ... There is no bare retire", pinned.

    The decision's whole safety argument is that an ORPHANED record is cleared by *acting* on
    the thing the row represents, never by dismissing the row. That is a claim about the size
    of this set: one event, and specifically the one that observes a kill. A second outgoing
    transition added later — an owner-initiated retire, a "dismiss" — fails here rather than
    being noticed in review.
    """
    escapes = {
        event for event in LifecycleEvent if (SessionState.ORPHANED, event) in LEGAL_TRANSITIONS
    }

    assert escapes == {LifecycleEvent.VERIFIED_FORCE_STOP}
    assert transition(SessionState.ORPHANED, LifecycleEvent.VERIFIED_FORCE_STOP).to_state is (
        SessionState.ENDED
    )
    for event in set(LifecycleEvent) - escapes:
        with pytest.raises(InvalidTransition):
            transition(SessionState.ORPHANED, event)


def test_untrusted_is_not_terminal_and_names_every_way_out() -> None:
    """DEC-020's shape, applied to the state a trust dialog puts a session in.

    A session waiting on a folder-trust dialog holds a live pane, so the record must be able
    to leave: the owner answers yes and it runs, the owner answers no and it ends, the agent
    gives up and it failed, the pane goes ambiguous and it orphans, or the owner force stops
    it. Each of those five is an *observed* act, which is the property this asserts — there is
    no bare dismissal that clears the row without something having happened to the pane.
    """
    escapes = {
        event for event in LifecycleEvent if (SessionState.UNTRUSTED, event) in LEGAL_TRANSITIONS
    }

    assert SessionState.UNTRUSTED not in TERMINAL_STATES
    assert escapes == {
        LifecycleEvent.READY,
        LifecycleEvent.STARTUP_ERROR,
        LifecycleEvent.TRUST_DECLINED,
        LifecycleEvent.VERIFIED_FORCE_STOP,
        LifecycleEvent.AMBIGUOUS_TERMINAL_EVIDENCE,
    }
    for event in set(LifecycleEvent) - escapes:
        with pytest.raises(InvalidTransition):
            transition(SessionState.UNTRUSTED, event)


def test_every_state_a_launch_can_be_in_can_reach_untrusted() -> None:
    """The dialog can be read late, so the correction has to be reachable from three states.

    `claude-remote` prints its readiness marker *before* the trust dialog, so whether the
    first capture that decides a launch sees the marker or the blocker is a race: the record
    can already be RUNNING, or already FAILED on a startup-budget timeout, when the dialog is
    finally observed. Both must be correctable, which is why TRUST_REQUIRED has three origins
    rather than only STARTING.
    """
    for origin in (SessionState.STARTING, SessionState.RUNNING, SessionState.FAILED):
        assert transition(origin, LifecycleEvent.TRUST_REQUIRED).to_state is (
            SessionState.UNTRUSTED
        )
