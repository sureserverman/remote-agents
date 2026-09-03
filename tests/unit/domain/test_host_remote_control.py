"""The host-level Remote Control snapshot, and the pairing secret that never prints.

Codex's Remote Control is a property of the shared app-server daemon, not of a pane, so its
snapshot carries a *connection* — what the daemon reports — and derives the lifecycle
`RemoteControlState` both surfaces already render from it. The derivation lives here, in the
domain, for the reason `TRUST_ANSWERABLE` does: the adapter, the application policy and both
surfaces must agree on it and none of them may import another.

`PairingCode` is the sharpest instance of DEC-013 -- what a provider hands this service is
rendered, never stored. A code that leaks through a `repr` into a log line or a traceback is
stored, whatever the storage layer does, so the type refuses to print itself.
"""

import dataclasses
from datetime import UTC, datetime

import pytest

from remote_agents.domain.remote_control import (
    HostConnection,
    HostRemoteControlStatus,
    PairingCode,
    RemoteControlState,
)

#: Written from the requirement, not from the implementation: what each daemon-reported
#: connection means for the one lifecycle vocabulary the surfaces render. CONNECTING counts
#: as ACTIVE because the enable already took -- the daemon is enrolled and the websocket is
#: still settling -- and DAEMON_ABSENT counts as UNKNOWN, not INACTIVE, because Codex
#: persists the enrollment preference: a stopped daemon is not evidence that this host is
#: not enrolled, and "off" is the direction an owner acts on by not acting.
DERIVED_STATE = {
    HostConnection.CONNECTED: RemoteControlState.ACTIVE,
    HostConnection.CONNECTING: RemoteControlState.ACTIVE,
    HostConnection.DISABLED: RemoteControlState.INACTIVE,
    HostConnection.DAEMON_ABSENT: RemoteControlState.UNKNOWN,
    HostConnection.ERRORED: RemoteControlState.UNKNOWN,
    HostConnection.UNREACHABLE: RemoteControlState.UNKNOWN,
}

EXPIRES = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def test_the_connection_vocabulary_is_closed() -> None:
    """Six connections, no more: an unlisted one has no declared state to derive."""
    assert {member.value for member in HostConnection} == {
        "daemon_absent",
        "disabled",
        "connecting",
        "connected",
        "errored",
        "unreachable",
    }


def test_every_connection_derives_exactly_one_state() -> None:
    """Exhaustive over the enum, so a sixth member fails here rather than rendering blank."""
    assert set(DERIVED_STATE) == set(HostConnection)
    for connection, expected in DERIVED_STATE.items():
        status = HostRemoteControlStatus.observed(connection, server_name="Paisleys-Blender")
        assert status.state is expected, connection
        assert status.connection is connection
        assert status.server_name == "Paisleys-Blender"


def test_a_status_whose_state_contradicts_its_connection_is_refused() -> None:
    """The pair is checked on construction; a hand-built mismatch never reaches a surface."""
    for connection, derived in DERIVED_STATE.items():
        for state in RemoteControlState:
            if state is derived:
                continue
            with pytest.raises(ValueError):
                HostRemoteControlStatus(state=state, connection=connection, server_name=None)


def test_the_agreeing_pair_constructs_directly() -> None:
    """`observed` is a convenience, not the only door -- the honest pair is constructible."""
    status = HostRemoteControlStatus(
        state=RemoteControlState.ACTIVE,
        connection=HostConnection.CONNECTED,
        server_name=None,
    )
    assert status.server_name is None


def test_the_status_is_frozen() -> None:
    status = HostRemoteControlStatus.observed(HostConnection.DISABLED, server_name=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        status.state = RemoteControlState.ACTIVE  # type: ignore[misc]


def test_the_pairing_code_is_frozen() -> None:
    code = PairingCode(code="ABCD-EFGH", expires_at=EXPIRES)
    with pytest.raises(dataclasses.FrozenInstanceError):
        code.code = "ZZZZ-9999"  # type: ignore[misc]


def test_the_pairing_code_is_readable_only_by_asking_for_it() -> None:
    """The value is reachable through the field, which is the one place that renders it."""
    assert PairingCode(code="ABCD-EFGH", expires_at=EXPIRES).code == "ABCD-EFGH"


def test_the_pairing_code_never_appears_in_its_own_repr_or_str() -> None:
    """DEC-013: a `repr` reaches logs and tracebacks nobody chose to write the secret into."""
    code = PairingCode(code="ABCD-EFGH", expires_at=EXPIRES)
    assert "ABCD" not in repr(code)
    assert "EFGH" not in repr(code)
    assert "ABCD" not in str(code)
    assert "EFGH" not in str(code)


def test_the_pairing_codes_repr_still_says_what_it_is() -> None:
    """Redaction is not silence -- an operator reading a log can tell what was withheld."""
    rendered = repr(PairingCode(code="ABCD-EFGH", expires_at=EXPIRES))
    assert "PairingCode" in rendered
    assert "redacted" in rendered.lower()


def test_an_empty_pairing_code_is_refused() -> None:
    """A blank code renders as a plausible screen and pairs nothing; it is a parse failure."""
    with pytest.raises(ValueError):
        PairingCode(code="", expires_at=EXPIRES)
