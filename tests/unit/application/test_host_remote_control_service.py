"""The host toggle is serialised, claimed once, and fails closed.

Three properties, and the reasons they are not interchangeable:

**Serialised**, because the subject is a single machine-wide daemon. Two enables racing is
not two harmless duplicates -- one of them can be the branch that brings a daemon up while
the other is asking it a question.

**Claimed**, because both surfaces deliver this action as a callback that a phone can
redeliver: Telegram retries, and a double-tap on a TUI key is one keypress the app pumped
twice. The claim is what makes "the owner pressed it once" true.

**Fail-closed**, because a boundary that raises where a surface expected a reading turns a
status line into a traceback. A `ProtocolError` is a reading -- the one that says the daemon
would not answer -- not an exception for a renderer to handle.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from remote_agents.adapters.agents.protocols import ProtocolError
from remote_agents.application.errors import DuplicateCommandError
from remote_agents.application.host_remote_control import (
    HostRemoteControlCommand,
    HostRemoteControlService,
    PairCommand,
)
from remote_agents.application.reconcile import SessionLocks
from remote_agents.domain.remote_control import (
    HostConnection,
    HostRemoteControlStatus,
    PairingCode,
    RemoteControlState,
)

EXPIRES = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class Store:
    """Records every write, so "stored nothing but the claim" is checkable."""

    def __init__(self) -> None:
        self.claims: set[str] = set()
        self.writes: list[tuple[str, object]] = []

    async def claim_idempotency_key(self, key: str) -> bool:
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    def __getattr__(self, name: str):
        async def recorder(*args, **kwargs):
            self.writes.append((name, args))

        return recorder


class FakeControl:
    """A scripted `HostRemoteControl`, recording the order it was driven in."""

    def __init__(
        self,
        connection: HostConnection = HostConnection.DISABLED,
        error: Exception | None = None,
    ) -> None:
        self.connection = connection
        self.error = error
        self.calls: list[str] = []
        self.entered = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.gate: asyncio.Event | None = None

    async def status(self) -> HostRemoteControlStatus:
        self.calls.append("status")
        if self.error is not None:
            raise self.error
        return HostRemoteControlStatus.observed(self.connection, server_name="Paisleys-Blender")

    async def set_state(self, desired: RemoteControlState) -> HostRemoteControlStatus:
        self.calls.append(f"set_state:{desired.value}")
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.error is not None:
                raise self.error
            return HostRemoteControlStatus.observed(
                HostConnection.CONNECTED
                if desired is RemoteControlState.ACTIVE
                else HostConnection.DISABLED,
                server_name="Paisleys-Blender",
            )
        finally:
            self.concurrent -= 1

    async def pair(self) -> PairingCode:
        self.calls.append("pair")
        if self.error is not None:
            raise self.error
        return PairingCode(code="ZZZZ-9999", expires_at=EXPIRES)


def service(control: FakeControl, store: Store | None = None) -> HostRemoteControlService:
    return HostRemoteControlService(control, store or Store(), SessionLocks())


# ------------------------------------------------------------------------------- commands


def test_a_command_cannot_ask_for_the_state_that_only_a_reading_produces() -> None:
    """`UNKNOWN` is what a reading says; there is no command that makes a daemon uncertain."""
    with pytest.raises(ValueError):
        HostRemoteControlCommand(RemoteControlState.UNKNOWN, "key-1")


def test_a_command_carries_no_session_because_its_subject_is_the_host() -> None:
    """The one structural difference from `RemoteControlCommand`, stated as a test."""
    fields = HostRemoteControlCommand(RemoteControlState.ACTIVE, "key-1").__dataclass_fields__
    assert "session_id" not in fields
    assert set(fields) == {"desired_state", "idempotency_key"}


# ------------------------------------------------------------------------------ set_state


async def test_setting_the_state_drives_the_port_and_answers_with_the_reading() -> None:
    control = FakeControl()
    result = await service(control).set_state(
        HostRemoteControlCommand(RemoteControlState.ACTIVE, "key-1")
    )

    assert control.calls == ["set_state:active"]
    assert result.connection is HostConnection.CONNECTED


async def test_the_same_command_delivered_twice_runs_once() -> None:
    """Telegram redelivers, and a double-tap is one keypress the app pumped twice."""
    control = FakeControl()
    subject = service(control)
    command = HostRemoteControlCommand(RemoteControlState.ACTIVE, "key-1")

    await subject.set_state(command)
    with pytest.raises(DuplicateCommandError):
        await subject.set_state(command)

    assert control.calls == ["set_state:active"], "the port must not be driven twice"


async def test_a_second_toggle_waits_for_the_first_rather_than_racing_it() -> None:
    """The subject is one machine-wide daemon, so two in flight is not two duplicates."""
    control = FakeControl()
    control.gate = asyncio.Event()
    subject = service(control)

    first = asyncio.create_task(
        subject.set_state(HostRemoteControlCommand(RemoteControlState.ACTIVE, "key-1"))
    )
    await asyncio.sleep(0)
    second = asyncio.create_task(
        subject.set_state(HostRemoteControlCommand(RemoteControlState.INACTIVE, "key-2"))
    )
    await asyncio.sleep(0)

    assert control.max_concurrent == 1, "the second toggle entered while the first was open"
    control.gate.set()
    await asyncio.gather(first, second)

    assert control.max_concurrent == 1
    assert control.calls == ["set_state:active", "set_state:inactive"]


async def test_a_refused_duplicate_never_reaches_the_port() -> None:
    """The claim is taken before the daemon is touched, not after."""
    store = Store()
    store.claims.add("key-1")
    control = FakeControl()

    with pytest.raises(DuplicateCommandError):
        await service(control, store).set_state(
            HostRemoteControlCommand(RemoteControlState.ACTIVE, "key-1")
        )
    assert control.calls == []


async def test_a_toggle_participates_in_the_shutdown_drain() -> None:
    """A daemon enable in flight must finish before the process stops admitting work."""
    locks = SessionLocks()
    control = FakeControl()
    control.gate = asyncio.Event()
    subject = HostRemoteControlService(control, Store(), locks)

    task = asyncio.create_task(
        subject.set_state(HostRemoteControlCommand(RemoteControlState.ACTIVE, "key-1"))
    )
    await asyncio.sleep(0)
    drain = asyncio.create_task(locks.drain())
    await asyncio.sleep(0)

    assert not drain.done(), "the drain finished while a toggle was still open"
    control.gate.set()
    await task
    await drain


# --------------------------------------------------------------------------------- status


async def test_reading_the_status_is_unlocked_and_takes_no_claim() -> None:
    """A surface rendering a list must not be able to block a toggle the owner is issuing."""
    store = Store()
    control = FakeControl(HostConnection.CONNECTED)

    result = await service(control, store).status()

    assert result.connection is HostConnection.CONNECTED
    assert store.claims == set(), "a read that claims a key would exhaust the key space"
    assert store.writes == []


async def test_a_boundary_that_will_not_answer_is_a_reading_not_an_exception() -> None:
    """A status line is rendered by a surface that has no branch for a traceback.

    UNREACHABLE rather than ERRORED: this is a failure to have the conversation, not a fact
    the daemon reported about itself.
    """
    control = FakeControl(error=ProtocolError("provider protocol is unavailable"))

    result = await service(control).status()

    assert result == HostRemoteControlStatus.observed(HostConnection.UNREACHABLE, server_name=None)
    assert result.state is RemoteControlState.UNKNOWN


async def test_a_toggle_whose_boundary_fails_also_answers_with_a_reading() -> None:
    """The same courtesy on the write path: the surface has one branch, not two."""
    control = FakeControl(error=ProtocolError("codex is not installed on this host"))

    result = await service(control).set_state(
        HostRemoteControlCommand(RemoteControlState.ACTIVE, "key-1")
    )

    assert result.connection is HostConnection.UNREACHABLE


# ----------------------------------------------------------------------------------- pair


async def test_pairing_returns_the_code_and_stores_nothing_but_the_claim() -> None:
    """DEC-013: what a provider hands this service is rendered, never stored."""
    store = Store()
    code = await service(FakeControl(), store).pair(PairCommand("pair-1"))

    assert code.code == "ZZZZ-9999"
    assert store.claims == {"pair-1"}
    assert store.writes == [], f"pairing wrote {store.writes}"


async def test_a_redelivered_pair_request_mints_no_second_code() -> None:
    """Two codes from one press is two live secrets, and only one is on the owner's screen."""
    control = FakeControl()
    subject = service(control)

    await subject.pair(PairCommand("pair-1"))
    with pytest.raises(DuplicateCommandError):
        await subject.pair(PairCommand("pair-1"))

    assert control.calls == ["pair"]


async def test_a_failed_pair_raises_rather_than_returning_a_hollow_code() -> None:
    """There is no honest empty `PairingCode`; a surface must render the failure."""
    control = FakeControl(error=ProtocolError("codex refused to mint a pairing code"))

    with pytest.raises(ProtocolError):
        await service(control).pair(PairCommand("pair-1"))


# ------------------------------------------------------------------------------- lifecycle


async def test_closing_the_service_reclaims_what_the_boundary_opened() -> None:
    """Codex's adapter keeps one `app-server proxy` child from the first status read on."""

    class ClosableControl(FakeControl):
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    control = ClosableControl()
    await service(control).aclose()

    assert control.closed is True


async def test_closing_a_service_whose_port_opens_nothing_is_not_an_error() -> None:
    """The port does not require `aclose`; a boundary that opens nothing has none."""
    await service(FakeControl()).aclose()


# ------------------------------------------------- a boundary we cannot reach at all


async def test_a_boundary_we_cannot_reach_is_not_reported_as_the_daemon_erroring() -> None:
    """The two are different facts and only one of them is the daemon's.

    ERRORED means the daemon answered and said its own connection is broken -- enrollment
    ON, socket down. UNREACHABLE means this project never had the conversation. Collapsing
    them told an owner on a host with no `codex` installed that Remote Control was
    "errored", which is a specific claim about a daemon that is not running, and left them
    pressing a button that could never explain itself.
    """
    control = FakeControl(error=ProtocolError("codex is not installed on this host"))

    result = await service(control).status()

    assert result.connection is HostConnection.UNREACHABLE
    assert result.connection is not HostConnection.ERRORED
    assert result.state is RemoteControlState.UNKNOWN


async def test_an_os_error_from_the_boundary_is_also_a_reading() -> None:
    """`status()` promises it never raises, so it may not depend on the adapter's diligence.

    The adapter converts `FileNotFoundError` and `TimeoutError`; a `PermissionError` from
    `create_subprocess_exec` was escaping as a bare `OSError` into a render path that has
    one branch for a reading and none for a traceback.
    """
    control = FakeControl(error=PermissionError("cannot exec codex"))

    result = await service(control).status()

    assert result.connection is HostConnection.UNREACHABLE


async def test_a_toggle_against_an_unreachable_boundary_is_also_a_reading() -> None:
    control = FakeControl(error=PermissionError("cannot exec codex"))

    result = await service(control).set_state(
        HostRemoteControlCommand(RemoteControlState.ACTIVE, "key-1")
    )

    assert result.connection is HostConnection.UNREACHABLE


# ------------------------------------------------------------------ shutdown ordering


async def test_closing_waits_for_an_in_flight_toggle_rather_than_racing_it() -> None:
    """Closing the proxy's pipes under a live `set_state` breaks the fail-closed promise.

    The mid-flight call would fail with something that is not `ProviderUnavailable`, so the
    `except` that exists to turn a failure into a reading would not catch it -- at exactly
    the moment this class's docstring says matters most.
    """

    class ClosableControl(FakeControl):
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    control = ClosableControl()
    control.gate = asyncio.Event()
    subject = service(control)

    toggle = asyncio.create_task(
        subject.set_state(HostRemoteControlCommand(RemoteControlState.ACTIVE, "key-1"))
    )
    await asyncio.sleep(0)
    closing = asyncio.create_task(subject.aclose())
    await asyncio.sleep(0)

    assert control.closed is False, "the close ran while a toggle held the lock"
    control.gate.set()
    await asyncio.gather(toggle, closing)
    assert control.closed is True


# ----------------------------------------------------- what a burned key actually costs


async def test_a_failed_toggle_still_burns_its_key() -> None:
    """Not a defect -- the safe ordering -- but pinned so it cannot change unnoticed.

    Claim-after-act would let a redelivered callback drive the daemon twice. The cost is
    that a redelivery of a press that failed is refused as "already handled". Both surfaces
    mint a fresh key per press, so the owner presses again and gets a real attempt.
    """
    store = Store()
    control = FakeControl(error=ProtocolError("unavailable"))
    subject = service(control, store)
    command = HostRemoteControlCommand(RemoteControlState.ACTIVE, "key-1")

    assert (await subject.set_state(command)).connection is HostConnection.UNREACHABLE
    assert store.claims == {"key-1"}
    with pytest.raises(DuplicateCommandError):
        await subject.set_state(command)


async def test_a_failed_pair_still_burns_its_key() -> None:
    """The same trade, with a sharper cost named in `pair`'s docstring."""
    store = Store()
    control = FakeControl(error=ProtocolError("codex refused to mint a pairing code"))
    subject = service(control, store)

    with pytest.raises(ProtocolError):
        await subject.pair(PairCommand("pair-1"))
    assert store.claims == {"pair-1"}
    with pytest.raises(DuplicateCommandError):
        await subject.pair(PairCommand("pair-1"))
