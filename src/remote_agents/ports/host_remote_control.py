"""The host-level Remote Control boundary -- one machine's daemon, not one session's pane.

Claude's Remote Control rides `ports/terminal.py`, because its subject is a live owned pane
and the terminal is what can address one. Codex's subject is the shared app-server daemon
this machine runs, which no pane owns and no session identifies, so it gets its own port
rather than a session-shaped argument nobody could supply (DEC-070: one capability, one
package, one descriptor field).

The port declares three verbs and deliberately not a fourth. Turning Remote Control off
flips a preference; it does not tear the daemon down, because every pane attached to that
daemon exits when it goes. A port that could express the destructive verb would invite an
adapter to implement it, so the port cannot say it -- and the CLI spellings that could are
adapter knowledge, named in exactly one module below this layer.

**Flipping the preference still restarts the daemon, and that is what `DaemonGuard` is for.**
Measured on codex-cli 0.162.1 (`docs/acceptance-2026-10-codex-enable-race.md`): every verb that
changes the preference stops the running backend and starts a new one. A pane past its daemon
handshake reconnects. One still inside it exits. A launch brings up the daemon itself, so a
launch and a flip landing together can kill the pane just launched (BL-037). The guard is one
cross-process lock that both sides hold: a launch until its handshake is drawn, a flip for its
whole run.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
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
    `DAEMON_ABSENT` and `ERRORED`, rather than raising for the ordinary ones. When one of
    them genuinely cannot answer it raises `ProviderUnavailable`, which is named in `ports/`
    precisely so a use case can catch it without reaching across the boundary (ARCH-02).
    """

    async def status(self) -> HostRemoteControlStatus:
        """This host's current Remote Control reading, without changing it."""
        ...

    async def set_state(self, desired: RemoteControlState) -> HostRemoteControlStatus:
        """Drive the host to `desired` and answer with the reading that followed.

        `ACTIVE` enrols this machine and starts the daemon if it is absent; `INACTIVE`
        disables remote control and leaves a daemon running. Either one restarts a running
        daemon when it changes the preference, so a caller holds `DaemonGuard` around it.
        `UNKNOWN` is not a destination -- it is what a reading says, never what a caller asks
        for -- and an implementation refuses it.
        """
        ...

    async def pair(self) -> PairingCode:
        """Mint a short-lived manual pairing code, for rendering once and never storing."""
        ...


class CodexRemoteControlBusy(RuntimeError):
    """The daemon guard stayed held past this caller's bound (BL-037).

    Raised before anything is changed: a launch refused here has no record and no pane, and a
    toggle refused here ran no `codex` command. Each surface says which side was waiting, so
    the message carries no owner-facing wording.
    """


class DaemonGuard(Protocol):
    """One lock between launches of the daemon's agent and changes to its Remote Control.

    Shared by launches, which do not race each other, and held alone by a change. Cross-process,
    because the bot's service and the local surface both launch and both toggle. `hold` waits
    at most `wait` seconds and then raises `CodexRemoteControlBusy`; a caller holding nothing
    else when it calls this cannot deadlock on it.
    """

    def hold(self, wait: float, *, alone: bool) -> AbstractAsyncContextManager[None]:
        """Hold the guard for the `async with` block, or raise once `wait` has passed.

        `alone=False` for a launch, resume or trust answer; `True` for a change.
        """
        ...
