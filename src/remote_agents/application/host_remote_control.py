"""The host-level Remote Control use case: policy, command types, and the service.

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

import asyncio
from dataclasses import dataclass

from remote_agents.application.errors import DuplicateCommandError
from remote_agents.application.reconcile import SessionLocks
from remote_agents.application.session_actions import REMOTE_CONTROL_LABELS
from remote_agents.domain.remote_control import (
    HostConnection,
    HostRemoteControlStatus,
    PairingCode,
    RemoteControlState,
)
from remote_agents.ports.host_remote_control import HostRemoteControl
from remote_agents.ports.provider_errors import ProviderUnavailable
from remote_agents.ports.session_store import SessionStore

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
    # Both, for the same reason ERRORED offers both and a different one besides: a boundary
    # that would not answer is frequently transient, and the owner pressing a direction is
    # how they find out whether it still will not. What must NOT happen is the surface
    # naming this "errored" -- see the member's own docstring.
    HostConnection.UNREACHABLE: (RemoteControlState.ACTIVE, RemoteControlState.INACTIVE),
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


@dataclass(frozen=True, slots=True)
class HostRemoteControlCommand:
    """One owner-issued host toggle, carrying no session because its subject is the host.

    That absence is the whole structural difference from `RemoteControlCommand`, and it is
    the reason this is a separate type rather than that one with an optional field: a
    session id that is sometimes meaningful is a field every reader has to ask about.
    """

    desired_state: RemoteControlState
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.desired_state is RemoteControlState.UNKNOWN:
            raise ValueError("remote control state must be active or inactive")


@dataclass(frozen=True, slots=True)
class PairCommand:
    """One owner-issued request to mint a pairing code."""

    idempotency_key: str


class HostRemoteControlService:
    """Drive this host's Remote Control: serialised, claimed once, and fail-closed.

    **Why a lock of its own, and what it does not give.** `SessionLocks.operation()` is a
    drain counter, not a mutex -- it increments, clears an event, and yields, so two callers
    inside it run concurrently by design. `SessionService`'s mutations get their exclusion
    from `for_session(...)` beside it, and a host action has no session to key on. So this
    service owns one lock, because it has exactly one subject: the machine.

    Stated precisely, since the neighbouring guarantee is easy to assume: this serialises
    *host toggles against each other*. It does **not** serialise a toggle against a session
    launch, and no lock available here would -- `launch` takes `operation()` alone, which
    excludes nothing. That matters because of the launch-order rule: a managed `codex` pane
    started while the daemon is coming up may end up embedded rather than daemon-backed, and
    so invisible to the phone. The honest mitigation is that the owner can see the reading
    and launch after it settles, not a lock this class could take.

    **The lock is per-process, and the subject is not.** `compose_backend` is called
    independently by the TUI and the bot compositions, each minting its own `SessionLocks`
    and its own service with its own lock. So this serialises host toggles *within one
    process*; an owner toggling from the terminal while the phone toggles from the bot gets
    two concurrent calls to one daemon, with distinct idempotency keys the shared SQLite
    claim cannot join. That is DEC-005's accepted multi-writer world and the operation
    converges rather than corrupting -- but it is stated here because the rest of this
    docstring is careful about what is not provided, and this is the qualifier that applies
    to its own lock.

    `operation()` is still entered, and earns its place for the other reason: an enable in
    flight is a network round trip that enrols this machine, and shutdown should finish it
    rather than abandon it half-done. **Stated honestly: nothing on the shipped `serve` path
    calls `SessionLocks.drain()` today** -- its only caller is `RuntimeCoordinator`, which
    `bootstrap` does not construct -- so entering `operation()` records the intent and buys
    the guarantee only once that gap is closed. It is a pre-existing gap that `SessionService`
    shares; it is named here rather than left implied by the word "drain".
    """

    def __init__(
        self, control: HostRemoteControl, store: SessionStore, locks: SessionLocks
    ) -> None:
        self._control = control
        self._store = store
        self._locks = locks
        self._host_lock = asyncio.Lock()

    async def aclose(self) -> None:
        """Reclaim whatever the provider boundary opened, if it can be reclaimed.

        Codex's adapter keeps one `codex app-server proxy` child alive from the first
        `status()` onward. That is one helper process per service, not one per call -- the
        composition builds exactly one of these -- so this is orderly shutdown rather than a
        leak being plugged. Tolerant of a port that has no `aclose`, because the protocol
        does not require one: a provider whose boundary opens nothing has nothing to close.
        """
        close = getattr(self._control, "aclose", None)
        if close is None:
            return
        # Under the same lock the toggles take. Closing the proxy's pipes out from under an
        # in-flight `set_state` would break this class's fail-closed promise at exactly the
        # moment its own docstring says matters most -- the mid-flight call would fail with
        # something that is not `ProviderUnavailable`, so the `except` that exists to turn a
        # failure into a reading would not catch it.
        async with self._host_lock:
            await close()

    async def status(self) -> HostRemoteControlStatus:
        """Read the host's Remote Control. Unlocked, unclaimed, and it never raises.

        Unlocked because a surface rendering a list would otherwise block a toggle the owner
        is trying to issue -- the reason `trust_state` is unlocked too. Unclaimed because a
        read that burns an idempotency key would exhaust the key space by being looked at.

        A `ProviderUnavailable` becomes UNREACHABLE rather than propagating: this answer is rendered
        into a status line by surfaces that have one branch for a reading and none for a
        traceback, and "the daemon would not answer" IS a reading.
        """
        try:
            return await self._control.status()
        except (ProviderUnavailable, OSError):
            return HostRemoteControlStatus.observed(HostConnection.UNREACHABLE, server_name=None)

    async def set_state(self, command: HostRemoteControlCommand) -> HostRemoteControlStatus:
        """Flip this host's Remote Control once for an exact owner press.

        A press that ends in an ERRORED or UNREACHABLE reading has still burned its key, so a
        redelivery of that same press is refused as "already handled" for something that
        demonstrably did not happen. Harmless because both surfaces mint a fresh key per
        press -- the owner presses again and gets a real attempt -- but written down because
        the two operations on this service read inconsistently side by side otherwise, and
        `pair`'s docstring explains the same trade in the opposite direction.
        """
        async with self._locks.operation(), self._host_lock:
            if not await self._store.claim_idempotency_key(command.idempotency_key):
                raise DuplicateCommandError("host remote control callback was already handled")
            try:
                return await self._control.set_state(command.desired_state)
            except (ProviderUnavailable, OSError):
                return HostRemoteControlStatus.observed(
                    HostConnection.UNREACHABLE, server_name=None
                )

    async def pair(self, command: PairCommand) -> PairingCode:
        """Mint one pairing code for one owner press, and store none of it.

        Claimed for a sharper reason than the toggle's: two codes minted from one press are
        two live secrets, and only one of them is on the owner's screen.

        **The key is burned before the code is minted, and stays burned if the mint fails.**
        That ordering is deliberate and it is the safe one -- claiming afterwards would let a
        redelivered callback mint a second secret -- but its cost is worth naming: a `pair`
        that fails validation *after* `codex` has already produced a code leaves a live
        secret nobody rendered and nobody can revoke from here. It expires on its own. Both
        surfaces mint a fresh key per press, so the owner is never stuck; the exposure is the
        unrendered code's lifetime, not the owner's ability to retry.

        Unlike the two paths above, a failure here RAISES. There is no honest empty
        `PairingCode` to answer with -- a surface must render "that did not work" rather
        than a box the owner would try to read out (DEC-013).
        """
        async with self._locks.operation(), self._host_lock:
            if not await self._store.claim_idempotency_key(command.idempotency_key):
                raise DuplicateCommandError("host pairing callback was already handled")
            return await self._control.pair()
