"""The host-level Remote Control boundary -- one machine's daemon, not one session's pane.

Claude's Remote Control rides `ports/terminal.py`, because its subject is a live owned pane
and the terminal is what can address one. Codex's subject is the shared app-server daemon
this machine runs, which no pane owns and no session identifies, so it gets its own port
rather than a session-shaped argument nobody could supply (DEC-070: one capability, one
package, one descriptor field).

The port declares three verbs and deliberately not a fourth. There is no `stop`: the Codex
CLI's `remote-control stop` kills the daemon, and every TUI attached to it exits on
disconnect -- so "turn Remote Control off" is `disable-remote-control`, which flips the
preference and leaves the panes alive. A port that could express the destructive verb would
invite an adapter to implement it.
"""

from __future__ import annotations

from typing import Protocol

from remote_agents.domain.remote_control import (
    HostRemoteControlStatus,
    PairingCode,
    RemoteControlState,
)


class HostRemoteControl(Protocol):
    """What one provider's host-level Remote Control can be asked.

    Implementations run outside the process and can fail in ways the caller must be able to
    render: `status()` answers with a snapshot for every reachable outcome, including
    `DAEMON_ABSENT` and `ERRORED`, rather than raising for the ordinary ones.
    """

    async def status(self) -> HostRemoteControlStatus:
        """This host's current Remote Control reading, without changing it."""
        ...

    async def set_state(self, desired: RemoteControlState) -> HostRemoteControlStatus:
        """Drive the host to `desired` and answer with the reading that followed.

        `ACTIVE` enrols this machine and starts the daemon if it is absent; `INACTIVE`
        disables remote control and leaves the daemon running, so attached panes survive.
        `UNKNOWN` is not a destination -- it is what a reading says, never what a caller asks
        for -- and an implementation refuses it.
        """
        ...

    async def pair(self) -> PairingCode:
        """Mint a short-lived manual pairing code, for rendering once and never storing."""
        ...
