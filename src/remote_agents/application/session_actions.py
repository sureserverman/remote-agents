"""The single authority over which lifecycle actions a session offers.

Action availability is a property of the lifecycle, not of whichever surface happens to
render it, so it lives here rather than in a driver adapter (DEC-001). Every adapter asks
this module and renders exactly what it returns; none of them branch on state themselves.

This reconciles three adapter-resident copies that had drifted apart: a list builder that
offered `force` from every state including STARTING, a token issuer that refused it unless
the state was one of RUNNING, STOP_REQUESTED, PRESERVED or FAILED, and an executor that
rechecked the live record against a fourth spelling of the same rule.

Availability is a *narrowing* of the domain's legal transitions, never a widening: an action
this module offers must be one `domain.state_machine` will actually perform, or the owner
confirms a destructive operation and receives an exception instead of a stopped session.
`tests/architecture/test_policy_matches_domain.py` enforces that direction.

ORPHANED is deliberately **not** forceable. It is tempting — it is the one state the two
prior copies never agreed on, and it can be stopped from no surface today — but the domain
has no transition out of ORPHANED at all, so a force from it raises InvalidTransition before
the terminal is reached. ORPHANED does not mean "the pane is gone" (that is
RECONCILED_TERMINAL_MISSING, which ends the session); it means the evidence was ambiguous, or
a tmux tag was found with no store row. `reconcile.py` quarantines it for local attention on
purpose. Making it forceable is a real capability with a real safety question — it would put
a one-tap kill on a possibly-live pane this app does not own — and it needs a domain
transition and a recorded decision, not a policy edit.
"""

from __future__ import annotations

from typing import Protocol

from remote_agents.domain.models import ProfileId, SessionState

GRACEFUL = "graceful"
CLEANUP = "cleanup"
FORCE = "force"

_FORCEABLE = frozenset(
    {
        SessionState.RUNNING,
        SessionState.STOP_REQUESTED,
        SessionState.PRESERVED,
        SessionState.FAILED,
    }
)


def available_actions(state: SessionState) -> tuple[str, ...]:
    """Return the stop actions offerable from `state`, destructive one last.

    STARTING offers nothing: the pane may not exist yet, and the domain has no stop
    transition from it; a stuck STARTING session is resolved by reconciliation instead.
    ENDED and ORPHANED offer nothing: both are read-only in the state machine.
    """
    actions: list[str] = []
    if state is SessionState.RUNNING:
        actions.append(GRACEFUL)
    if state is SessionState.PRESERVED:
        actions.append(CLEANUP)
    if state in _FORCEABLE:
        actions.append(FORCE)
    return tuple(actions)


_EXPLANATIONS = {
    SessionState.STARTING: "The agent is starting; it is not ready to act on yet.",
    SessionState.RUNNING: "The agent is running.",
    SessionState.STOP_REQUESTED: "A graceful stop is in progress.",
    SessionState.PRESERVED: "The agent exited; its output is preserved for inspection.",
    SessionState.FAILED: (
        "The session did not become ready. Its pane may still exist; a later readiness "
        "check can still promote it."
    ),
    SessionState.ENDED: "The session is closed; nothing is left to reach.",
    # Two producers, and the wording has to fit both: a record whose pane evidence was
    # neither live nor preserved (reconcile.py "ambiguous_terminal"), and a trusted pane
    # found with no record at all ("unknown_session"). It is quarantined either way, and
    # nothing the owner does moves it out — there is no transition from ORPHANED.
    SessionState.ORPHANED: (
        "This session and the panes on this host could not be reconciled, so it is held "
        "aside. No action is offered for it."
    ),
}


def explain_state(state: SessionState) -> str:
    """One line describing `state` to the owner, for any surface that renders a session.

    Every member is spelled out rather than defaulted. The bot previously classified four
    states and fell back to "This session is no longer active." for the rest, which was
    false for STARTING — a session that is actively coming up.
    """
    return _EXPLANATIONS[state]


class _RemoteControllable(Protocol):
    """The two fields availability turns on; any session record satisfies this."""

    profile_id: ProfileId
    state: SessionState


def remote_control_available(record: _RemoteControllable) -> bool:
    """Whether a surface should offer the Claude Remote Control toggle for `record`.

    Only Claude implements the pane action, and only a live pane can receive it.

    The two axes are defended unequally, which matters for any surface built on this.
    `SessionService.set_remote_control` re-checks the **profile** independently, so a caller
    that skips this function still cannot drive a non-Claude pane. It does **not** check the
    state: this function is the only thing standing between a caller and a Remote Control
    toggle on a STARTING, STOP_REQUESTED or ORPHANED Claude session. Consult it before
    offering the toggle; do not treat the service as a backstop for the state half.
    """
    return record.profile_id == ProfileId("claude") and record.state is SessionState.RUNNING
