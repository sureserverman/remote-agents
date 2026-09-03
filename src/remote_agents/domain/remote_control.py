"""Typed state for the Remote Control actions -- Claude's pane toggle and Codex's host one.

Two subjects, one vocabulary. Claude's Remote Control is a property of a live owned pane
(DEC-003); Codex's is a property of the shared app-server daemon this machine runs, so it has
no session to hang off. Both render the same `RemoteControlState`, which is why the host
snapshot *derives* its state from the daemon's connection rather than carrying an
independent one: a surface that can already say "on / off / unknown" needs no second word
for the same fact (DEC-007).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class RemoteControlState(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class HostConnection(StrEnum):
    """What the Codex app-server daemon reports about its remote-control enrollment.

    `DAEMON_ABSENT` is this project's own member, not the daemon's: `codex app-server daemon
    version` answers with a connect failure when nothing is listening, and that is a fact
    about the host worth rendering rather than an error worth raising.
    """

    DAEMON_ABSENT = "daemon_absent"
    DISABLED = "disabled"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERRORED = "errored"


#: The one derivation, in the domain because the adapter, the application policy and both
#: surfaces must agree on it and none of them may import another (DEC-001). CONNECTING is
#: ACTIVE because the enable has already taken and only the websocket is still settling;
#: DAEMON_ABSENT is INACTIVE because a daemon that is not running remote-controls nothing;
#: ERRORED is UNKNOWN because the daemon answered and declined to say, which is exactly what
#: the third word is for.
_DERIVED_STATE: dict[HostConnection, RemoteControlState] = {
    HostConnection.CONNECTED: RemoteControlState.ACTIVE,
    HostConnection.CONNECTING: RemoteControlState.ACTIVE,
    HostConnection.DISABLED: RemoteControlState.INACTIVE,
    HostConnection.DAEMON_ABSENT: RemoteControlState.INACTIVE,
    HostConnection.ERRORED: RemoteControlState.UNKNOWN,
}


@dataclass(frozen=True, slots=True)
class HostRemoteControlStatus:
    """One reading of this host's Codex Remote Control, as the daemon reported it.

    `server_name` is the daemon's name for this machine -- the string the phone shows, and a
    string this project did not decode, so every surface passes it through the presentation
    boundary encoder before rendering it (DEC-014).
    """

    state: RemoteControlState
    connection: HostConnection
    server_name: str | None

    def __post_init__(self) -> None:
        derived = _DERIVED_STATE[self.connection]
        if self.state is not derived:
            raise ValueError(
                f"{self.connection} derives {derived}, not {self.state} -- a status whose "
                "state contradicts its connection would render one word while meaning "
                "another"
            )

    @classmethod
    def observed(
        cls, connection: HostConnection, *, server_name: str | None
    ) -> "HostRemoteControlStatus":
        """The snapshot for a connection the daemon reported, with its state derived."""
        return cls(
            state=_DERIVED_STATE[connection],
            connection=connection,
            server_name=server_name,
        )


@dataclass(frozen=True, slots=True, repr=False)
class PairingCode:
    """A short-lived manual pairing code, rendered once and never stored (DEC-013).

    The type refuses to print itself. A `repr` is not a rendering decision anyone makes on
    purpose -- it arrives through a log record, an f-string in an exception, a debugger dump
    -- so the redaction lives on the value rather than in the discipline of every caller.
    Reading the secret takes asking for `.code`, which is one grep away from every site that
    does it.
    """

    code: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("a pairing code with no value pairs nothing")

    def __repr__(self) -> str:
        return f"PairingCode(code=<redacted>, expires_at={self.expires_at!r})"

    def __str__(self) -> str:
        return self.__repr__()
