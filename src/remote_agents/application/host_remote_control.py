"""Availability, direction and labels for the host-level Remote Control toggle.

A sibling of `session_actions.remote_control_directions`, not a generalisation of it. The
pane toggle's subject is one live owned Claude session; this one's subject is the machine.
Folding them into a single function would produce one whose argument means two different
things depending on who called it, and both surfaces would then have to know which.

What the two *do* share is the vocabulary, and the sharing is by identity rather than by
agreement: `HOST_REMOTE_CONTROL_LABELS` **is** `REMOTE_CONTROL_LABELS`. The bot and the
terminal once spelled the pane toggle's labels identically by coincidence, and the note on
that table records why a coincidence is not a contract. Re-stating the same two strings here
would have recreated exactly the drift it was written to end (DEC-007).
"""

from __future__ import annotations

from remote_agents.application.session_actions import REMOTE_CONTROL_LABELS
from remote_agents.domain.remote_control import (
    HostConnection,
    HostRemoteControlStatus,
    RemoteControlState,
)

#: What each direction is called on screen -- the pane toggle's table, by identity.
HOST_REMOTE_CONTROL_LABELS = REMOTE_CONTROL_LABELS

#: What the fact itself is called. Named for the provider because the subject is the host:
#: an owner who already knows the Claude pane toggle would otherwise read a bare "Remote
#: Control" as that one, and act on the wrong machine-versus-pane assumption.
HOST_REMOTE_CONTROL_TITLE = "Codex Remote Control"

#: The connections in which there is a live relay link to pair a phone *to*. Pairing while
#: disabled would mint a code that expires unused, which reads to an owner as a broken
#: feature rather than as an action that was never available.
_PAIRABLE = frozenset({HostConnection.CONNECTED, HostConnection.CONNECTING})

#: The direction worth offering for each reading. ERRORED offers both for the reason the
#: pane policy offers both on an unknown observation: acting on a guess is the failure this
#: kind of function exists to avoid, not to introduce. DAEMON_ABSENT offers "on" because
#: that is the action that would make the host reachable, even though its *state* is UNKNOWN
#: -- which is why this table keys off the connection and not the derived state.
_DIRECTIONS: dict[HostConnection, tuple[RemoteControlState, ...]] = {
    HostConnection.CONNECTED: (RemoteControlState.INACTIVE,),
    HostConnection.CONNECTING: (RemoteControlState.INACTIVE,),
    HostConnection.DISABLED: (RemoteControlState.ACTIVE,),
    HostConnection.DAEMON_ABSENT: (RemoteControlState.ACTIVE,),
    HostConnection.ERRORED: (RemoteControlState.ACTIVE, RemoteControlState.INACTIVE),
}


def host_remote_control_directions(
    status: HostRemoteControlStatus | None,
) -> tuple[RemoteControlState, ...]:
    """Which directions a surface should offer for this host's Remote Control.

    `None` means the composition wired no host-level toggle at all -- a declared absence
    (DEC-061), not a failure -- and the answer is `()`. Returning that rather than raising
    lets a surface render this without first consulting a separate availability predicate it
    could then disagree with, which is the shape `remote_control_directions` already uses.
    """
    if status is None:
        return ()
    return _DIRECTIONS[status.connection]


def pair_available(status: HostRemoteControlStatus | None) -> bool:
    """Whether offering `Pair` would produce a code that can actually pair anything."""
    if status is None:
        return False
    return status.connection in _PAIRABLE
