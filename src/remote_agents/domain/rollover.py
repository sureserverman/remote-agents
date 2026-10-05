"""The rollover lifecycle: legal moves, restart recovery, and the one guard on stopping.

A rollover replaces a managed session (the predecessor) with a fresh one (the successor) that
adopts the predecessor's work. The order is the whole point: the successor is launched and must
accept before the predecessor is touched, so a failure anywhere before acceptance leaves the
predecessor running exactly as it was. Everything here is pure so the application pass, the
restart path and the tests all read the same table.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class RolloverState(StrEnum):
    """Where a rollover stands; persisted, so values are stable strings."""

    REQUESTED = "requested"
    HANDOFF_READY = "handoff_ready"
    SUCCESSOR_STARTING = "successor_starting"
    ADOPTING = "adopting"
    SUCCESSOR_ACCEPTED = "successor_accepted"
    PREDECESSOR_STOPPING = "predecessor_stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    STOP_FAILED = "stop_failed"
    CANCELLED = "cancelled"


SUCCESSOR_UNKNOWN = "successor-unknown"
"""Failure code when a restart finds a launch in flight with no recorded successor."""

SUCCESSOR_FAILED = "successor-failed"
"""Failure code when the recorded successor's pane is gone before it accepted."""


_S = RolloverState

TRANSITIONS: dict[RolloverState, frozenset[RolloverState]] = {
    _S.REQUESTED: frozenset({_S.HANDOFF_READY, _S.CANCELLED, _S.FAILED}),
    # Cancelling is safe here because nothing has been launched yet; from SUCCESSOR_STARTING
    # on, a successor may exist, so the only way out short of acceptance is FAILED.
    _S.HANDOFF_READY: frozenset({_S.SUCCESSOR_STARTING, _S.FAILED, _S.CANCELLED}),
    _S.SUCCESSOR_STARTING: frozenset({_S.ADOPTING, _S.FAILED}),
    _S.ADOPTING: frozenset({_S.SUCCESSOR_ACCEPTED, _S.FAILED}),
    # After acceptance there is no FAILED: the successor owns the work, so the only open
    # question is whether the predecessor stopped. A stop that does not verify is STOP_FAILED
    # and the owner uses force stop (DEC-007), which the rollover never does itself.
    _S.SUCCESSOR_ACCEPTED: frozenset({_S.PREDECESSOR_STOPPING}),
    _S.PREDECESSOR_STOPPING: frozenset({_S.COMPLETED, _S.STOP_FAILED}),
}
"""The legal-move matrix. No self-loops; a state absent as a key has no way out."""


TERMINAL: frozenset[RolloverState] = frozenset(
    state for state in RolloverState if not TRANSITIONS.get(state)
)
"""States the matrix offers no way out of, derived so the two cannot drift apart."""


NEEDS_SUCCESSOR: frozenset[RolloverState] = frozenset(
    {
        _S.ADOPTING,
        _S.SUCCESSOR_ACCEPTED,
        _S.PREDECESSOR_STOPPING,
        _S.COMPLETED,
        _S.STOP_FAILED,
    }
)
"""States a rollover cannot be in without a recorded successor: from ADOPTING on, everything
that happens is about one particular session this service launched."""


def is_legal(from_state: RolloverState, to_state: RolloverState) -> bool:
    """Return whether the matrix allows moving from ``from_state`` to ``to_state``."""
    return to_state in TRANSITIONS.get(from_state, frozenset())


class RecoveryAction(StrEnum):
    """What a restarted pass does with a rollover it finds persisted."""

    NOTHING = "nothing"
    WAIT = "wait"
    LAUNCH = "launch"
    GIVE_UP_SUCCESSOR_UNKNOWN = "give_up_successor_unknown"
    GIVE_UP_SUCCESSOR_FAILED = "give_up_successor_failed"
    TIME_OUT = "time_out"
    STOP_PREDECESSOR = "stop_predecessor"
    RECONCILE_STOP = "reconcile_stop"


@dataclass(frozen=True, slots=True)
class RecoveryFacts:
    """What the restart path observed; only the fields a row reads matter to it."""

    has_successor_id: bool = False
    successor_alive: bool = False
    timed_out: bool = False


def recovery_action(state: RolloverState, facts: RecoveryFacts) -> RecoveryAction:
    """Return the restart-recovery action for ``state`` given ``facts`` (brief §26).

    A launch is only ever issued from HANDOFF_READY with no successor recorded. A launch found
    in flight without a successor id is given up, never retried: there is no lookup from the
    idempotency key to a session, so a retry could start a second successor.
    """
    match state:
        case _S.REQUESTED:
            return RecoveryAction.WAIT
        case _S.HANDOFF_READY:
            # The successor id is recorded only once SUCCESSOR_STARTING is persisted (the store's
            # `record_successor`), so one here is a contradiction; giving up is the reading
            # that cannot launch twice.
            if facts.has_successor_id:
                return RecoveryAction.GIVE_UP_SUCCESSOR_UNKNOWN
            return RecoveryAction.LAUNCH
        case _S.SUCCESSOR_STARTING:
            if not facts.has_successor_id:
                return RecoveryAction.GIVE_UP_SUCCESSOR_UNKNOWN
            if facts.successor_alive:
                return RecoveryAction.WAIT
            return RecoveryAction.GIVE_UP_SUCCESSOR_FAILED
        case _S.ADOPTING:
            return RecoveryAction.TIME_OUT if facts.timed_out else RecoveryAction.WAIT
        case _S.SUCCESSOR_ACCEPTED:
            return RecoveryAction.STOP_PREDECESSOR
        case _S.PREDECESSOR_STOPPING:
            return RecoveryAction.RECONCILE_STOP
        case _:
            return RecoveryAction.NOTHING


class _HasState(Protocol):
    @property
    def state(self) -> RolloverState: ...


_STOP_ALLOWED: frozenset[RolloverState] = frozenset(
    {_S.SUCCESSOR_ACCEPTED, _S.PREDECESSOR_STOPPING}
)


def may_stop_predecessor(rollover: RolloverState | _HasState) -> bool:
    """Return whether the predecessor may be stopped; true only once the successor accepted.

    Takes a state or any object with a ``.state`` (a persisted rollover row). This is the sole
    gate on stopping the predecessor: every earlier state, and every failure, leaves it running.
    A row that also carries ``successor_session_id`` licenses a stop only with one recorded:
    "accepted" means accepted by a particular session, and a row naming none names no one.
    """
    if isinstance(rollover, RolloverState):
        return rollover in _STOP_ALLOWED
    if getattr(rollover, "successor_session_id", True) is None:
        return False
    return rollover.state in _STOP_ALLOWED
