"""The sole authority for legal managed-session lifecycle transitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from remote_agents.domain.models import SessionState


class LifecycleEvent(StrEnum):
    """Trusted events that may cause a persisted state transition."""

    READY = "ready"
    STARTUP_ERROR = "startup_error"
    GRACEFUL_STOP_REQUESTED = "graceful_stop_requested"
    PANE_EXITED = "pane_exited"
    GRACEFUL_STOP_TIMED_OUT = "graceful_stop_timed_out"
    VERIFIED_FORCE_STOP = "verified_force_stop"
    CLEANUP_CONFIRMED = "cleanup_confirmed"
    AMBIGUOUS_TERMINAL_EVIDENCE = "ambiguous_terminal_evidence"
    RECONCILED_TERMINAL_MISSING = "reconciled_terminal_missing"
    RECONCILED_PANE_DEAD = "reconciled_pane_dead"


@dataclass(frozen=True, slots=True)
class Transition:
    """A valid state change approved by the lifecycle matrix."""

    from_state: SessionState
    event: LifecycleEvent
    to_state: SessionState


class InvalidTransition(ValueError):
    """Raised when an event is not legal for a session's current state."""


_TRANSITIONS: dict[tuple[SessionState, LifecycleEvent], SessionState] = {
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


TERMINAL_STATES: frozenset[SessionState] = frozenset(
    state for state in SessionState if not any(origin is state for origin, _ in _TRANSITIONS)
)
"""States the matrix offers no way out of, derived so the two cannot drift apart."""


def transition(from_state: SessionState, event: LifecycleEvent) -> Transition:
    """Return the only legal transition for ``from_state`` and ``event``.

    In particular, a graceful-stop timeout never escalates to force stop: it restores
    ``RUNNING`` so the separately confirmed force-stop action remains explicit.
    """
    try:
        to_state = _TRANSITIONS[(from_state, event)]
    except KeyError as error:
        raise InvalidTransition(
            f"{event.value} is not legal while session is {from_state.value}"
        ) from error
    return Transition(from_state=from_state, event=event, to_state=to_state)
