"""The single authority over which lifecycle actions a session offers.

Action availability is a property of the lifecycle, not of whichever surface happens to
render it, so it lives here rather than in a driver adapter (DEC-001). Every adapter asks
this module and renders exactly what it returns; none of them branch on state themselves.

This reconciles two adapter-resident copies that had drifted apart: a list builder that
offered `force` from every state including STARTING, and a token issuer that refused it
unless the state was one of RUNNING, STOP_REQUESTED, PRESERVED or FAILED. The disagreement
was invisible because the refusal silently won, which left an ORPHANED session offering no
stop at all. ORPHANED is forceable here — a session whose pane is gone still holds a store
record, and force is the only action that can retire it.
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
        SessionState.ORPHANED,
    }
)


def available_actions(state: SessionState) -> tuple[str, ...]:
    """Return the stop actions offerable from `state`, destructive one last.

    STARTING offers nothing: the pane may not exist yet, so there is nothing to stop and
    no record worth retiring. ENDED offers nothing: the lifecycle is already closed.
    """
    actions: list[str] = []
    if state is SessionState.RUNNING:
        actions.append(GRACEFUL)
    if state is SessionState.PRESERVED:
        actions.append(CLEANUP)
    if state in _FORCEABLE:
        actions.append(FORCE)
    return tuple(actions)


class _RemoteControllable(Protocol):
    """The two fields availability turns on; any session record satisfies this."""

    profile_id: ProfileId
    state: SessionState


def remote_control_available(record: _RemoteControllable) -> bool:
    """Whether a surface should offer the Claude Remote Control toggle for `record`.

    Only Claude implements the pane action, and only a live pane can receive it. This is
    the presentation gate; `SessionService.set_remote_control` keeps its own independent
    profile check so a surface that ignores this one still cannot drive a non-Claude pane.
    """
    return record.profile_id == ProfileId("claude") and record.state is SessionState.RUNNING
