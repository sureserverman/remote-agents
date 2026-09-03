"""The Codex daemon boundary: a closed argv table, injected runners, and no teardown verb.

This is the one place this project runs a `codex` command whose effect reaches past a pane --
`set_state(ACTIVE)` enrols this machine with OpenAI's relay -- so the tests here are as much
about what the adapter *cannot* do as what it does. Nothing below `tests/live` spawns a real
process: both collaborators are injected and both fakes record every call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.agents.codex.remote_control import (
    REMOTE_CONTROL_ARGV,
    CodexRemoteControl,
    CommandResult,
)
from remote_agents.adapters.agents.protocols import ProtocolError
from remote_agents.domain.remote_control import (
    HostConnection,
    HostRemoteControlStatus,
    PairingCode,
    RemoteControlState,
)

FIXTURES = Path(__file__).resolve().parents[4] / "provider_contract" / "fixtures" / "codex"


def fixture(name: str) -> str:
    return (FIXTURES / "remote_control" / name).read_text(encoding="utf-8")


@dataclass
class FakeRunner:
    """Answers a fixed argv with a scripted result, recording argv and timeout for each."""

    results: dict[tuple[str, ...], CommandResult] = field(default_factory=dict)
    calls: list[tuple[tuple[str, ...], float]] = field(default_factory=list)
    default: CommandResult = CommandResult(returncode=0, stdout="{}", stderr="")

    async def run(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult:
        self.calls.append((argv, timeout))
        return self.results.get(argv, self.default)

    @property
    def argvs(self) -> list[tuple[str, ...]]:
        return [argv for argv, _ in self.calls]


@dataclass
class FakeRpc:
    """One JSON-RPC round trip, scripted per method, recording what was asked."""

    payload: object = field(default_factory=dict)
    error: Exception | None = None
    calls: list[tuple[str, dict]] = field(default_factory=list)

    async def request(self, method: str, params: dict) -> dict:
        self.calls.append((method, dict(params)))
        if self.error is not None:
            raise self.error
        return self.payload  # type: ignore[return-value]


def adapter(runner: FakeRunner | None = None, rpc: FakeRpc | None = None) -> CodexRemoteControl:
    return CodexRemoteControl(runner=runner or FakeRunner(), rpc=rpc or FakeRpc())


# --------------------------------------------------------------------------- the argv table


def test_the_argv_table_is_a_closed_module_constant_that_only_runs_codex() -> None:
    assert REMOTE_CONTROL_ARGV, "an empty table would pass every assertion below vacuously"
    for name, argv in REMOTE_CONTROL_ARGV.items():
        assert isinstance(argv, tuple), name
        assert argv[0] == "codex", name


def test_no_entry_in_the_table_can_tear_the_daemon_down() -> None:
    """The teardown verb kills the daemon and every attached pane exits on disconnect.

    Written over the whole table rather than over the entries that exist today, so an entry
    added later is covered before anyone writes a case for it.
    """
    assert not any("stop" in argv for argv in REMOTE_CONTROL_ARGV.values())


# ------------------------------------------------------------------------------- status()


CONNECTION_FOR_REPORTED_STATUS = {
    "connected": HostConnection.CONNECTED,
    "connecting": HostConnection.CONNECTING,
    "disabled": HostConnection.DISABLED,
    "errored": HostConnection.ERRORED,
}


async def test_status_asks_the_daemon_through_the_app_server_proxy() -> None:
    rpc = FakeRpc(payload={"status": "connected", "serverName": "Paisleys-Blender"})
    result = await adapter(rpc=rpc).status()

    assert rpc.calls == [("remoteControl/status/read", {})]
    assert REMOTE_CONTROL_ARGV["status"] == ("codex", "app-server", "proxy")
    assert result.connection is HostConnection.CONNECTED
    assert result.server_name == "Paisleys-Blender"


@pytest.mark.parametrize(("reported", "expected"), sorted(CONNECTION_FOR_REPORTED_STATUS.items()))
async def test_every_status_the_daemon_reports_maps_to_one_connection(
    reported: str, expected: HostConnection
) -> None:
    rpc = FakeRpc(payload={"status": reported, "serverName": "Paisleys-Blender"})
    assert (await adapter(rpc=rpc).status()).connection is expected


async def test_a_status_the_daemon_has_never_reported_is_a_protocol_error() -> None:
    rpc = FakeRpc(payload={"status": "teleported", "serverName": "x"})
    with pytest.raises(ProtocolError):
        await adapter(rpc=rpc).status()


async def test_a_connect_failure_naming_the_control_socket_means_no_daemon() -> None:
    """`codex app-server daemon version` answers with that path when nothing is listening."""
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["daemon_probe"]: CommandResult(
                returncode=1,
                stdout="",
                stderr=(
                    "Error: failed to connect to "
                    "/home/user/.codex/app-server-control/app-server-control.sock"
                ),
            )
        }
    )
    rpc = FakeRpc(error=ProtocolError("provider protocol is unavailable"))

    result = await adapter(runner=runner, rpc=rpc).status()

    assert result.connection is HostConnection.DAEMON_ABSENT
    assert result.state is RemoteControlState.INACTIVE
    assert result.server_name is None


async def test_any_other_rpc_failure_is_raised_rather_than_read_as_no_daemon() -> None:
    """A daemon that is up and failing must not render as a daemon that is not there."""
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["daemon_probe"]: CommandResult(
                returncode=0, stdout='{"cli":"0.151.0"}', stderr=""
            )
        }
    )
    rpc = FakeRpc(error=ProtocolError("provider protocol response timed out"))

    with pytest.raises(ProtocolError):
        await adapter(runner=runner, rpc=rpc).status()


async def test_the_server_name_passes_the_presentation_boundary_encoder() -> None:
    """DEC-014: a name this project did not decode is made encodable once, here."""
    rpc = FakeRpc(payload={"status": "connected", "serverName": "Pais\x1b[31mleys\x07"})
    result = await adapter(rpc=rpc).status()

    assert result.server_name is not None
    assert "\x1b" not in result.server_name
    assert "\x07" not in result.server_name


async def test_a_reading_with_no_server_name_says_so_rather_than_inventing_one() -> None:
    rpc = FakeRpc(payload={"status": "disabled"})
    assert (await adapter(rpc=rpc).status()).server_name is None


async def test_the_recorded_connected_fixture_reads_as_connected() -> None:
    rpc = FakeRpc(payload=json.loads(fixture("status-connected.json")))
    assert (await adapter(rpc=rpc).status()).connection is HostConnection.CONNECTED


# --------------------------------------------------------------------------- set_state()


async def test_turning_it_on_runs_exactly_the_start_argv() -> None:
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["enable"]: CommandResult(
                returncode=0, stdout=fixture("start-connecting.json"), stderr=""
            )
        }
    )
    result = await adapter(runner=runner).set_state(RemoteControlState.ACTIVE)

    assert runner.argvs == [("codex", "remote-control", "start", "--json")]
    assert result.connection is HostConnection.CONNECTING
    assert result.state is RemoteControlState.ACTIVE
    assert result.server_name == "Paisleys-Blender"


async def test_a_failed_enable_is_errored_and_does_not_echo_what_codex_printed() -> None:
    """Provider stderr is rendered by nothing here: it can carry a path, a token, a prompt."""
    secret = "token=sk-abcdef0123456789"
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["enable"]: CommandResult(
                returncode=1, stdout="", stderr=f"auth failed: {secret}"
            )
        }
    )
    result = await adapter(runner=runner).set_state(RemoteControlState.ACTIVE)

    # Equality with the canonical reading, not just "the secret is absent": a failed enable
    # must produce the *same* value whatever codex printed, which no interpolation of stderr
    # into any field can satisfy. The weaker `secret not in repr(...)` form would also hold
    # for a branch that never read stderr at all, and so could not tell the two apart.
    assert result == HostRemoteControlStatus.observed(HostConnection.ERRORED, server_name=None)
    assert result.state is RemoteControlState.UNKNOWN
    assert secret not in repr(result)
    assert secret not in str(result)


async def test_an_enable_whose_json_is_unreadable_is_errored_and_echoes_nothing() -> None:
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["enable"]: CommandResult(
                returncode=0, stdout="not json at all: /home/user/secret", stderr=""
            )
        }
    )
    result = await adapter(runner=runner).set_state(RemoteControlState.ACTIVE)

    assert result.connection is HostConnection.ERRORED
    assert "secret" not in repr(result)


async def test_turning_it_off_disables_the_preference_and_re_reads_the_status() -> None:
    """`off` never tears the daemon down -- attached panes would exit on disconnect."""
    runner = FakeRunner()
    rpc = FakeRpc(payload={"status": "disabled", "serverName": "Paisleys-Blender"})

    result = await adapter(runner=runner, rpc=rpc).set_state(RemoteControlState.INACTIVE)

    assert runner.argvs == [("codex", "app-server", "daemon", "disable-remote-control")]
    assert rpc.calls == [("remoteControl/status/read", {})]
    assert result.connection is HostConnection.DISABLED


async def test_a_failed_disable_is_errored_and_does_not_re_read() -> None:
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["disable"]: CommandResult(
                returncode=1, stdout="", stderr="no such daemon /tmp/private"
            )
        }
    )
    rpc = FakeRpc(payload={"status": "connected"})

    result = await adapter(runner=runner, rpc=rpc).set_state(RemoteControlState.INACTIVE)

    assert result == HostRemoteControlStatus.observed(HostConnection.ERRORED, server_name=None)
    assert rpc.calls == [], "a failed disable must not be reported as a re-read reading"
    assert "private" not in repr(result)
    assert "private" not in str(result)


async def test_unknown_is_a_reading_not_a_destination() -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError):
        await adapter(runner=runner).set_state(RemoteControlState.UNKNOWN)
    assert runner.calls == [], "a refused request must not have run anything"


# ------------------------------------------------------------------------------- pair()


async def test_pairing_runs_the_pair_argv_and_returns_the_manual_code() -> None:
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["pair"]: CommandResult(
                returncode=0, stdout=fixture("pair.json"), stderr=""
            )
        }
    )
    code = await adapter(runner=runner).pair()

    assert runner.argvs == [("codex", "remote-control", "pair", "--json")]
    assert isinstance(code, PairingCode)
    assert code.code == "ZZZZ-9999", "the manual code is the one the app's manual screen takes"
    assert code.expires_at == datetime.fromtimestamp(1788436800, tz=UTC)


async def test_pairing_prefers_the_manual_code_over_the_short_one() -> None:
    """`pairing_code` is the app-to-app handshake; only the manual code is typed by a human."""
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["pair"]: CommandResult(
                returncode=0,
                stdout=json.dumps(
                    {
                        "pairing_code": "0000-0000",
                        "manual_pairing_code": "ZZZZ-9999",
                        "expires_at": 1788436800,
                    }
                ),
                stderr="",
            )
        }
    )
    assert (await adapter(runner=runner).pair()).code == "ZZZZ-9999"


async def test_a_pair_response_without_a_manual_code_is_refused() -> None:
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["pair"]: CommandResult(
                returncode=0,
                stdout=json.dumps({"pairing_code": "0000-0000", "expires_at": 1788436800}),
                stderr="",
            )
        }
    )
    with pytest.raises(ProtocolError):
        await adapter(runner=runner).pair()


async def test_a_pair_response_without_an_expiry_is_refused() -> None:
    """A code with no expiry would render as one that never expires, which it is not."""
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["pair"]: CommandResult(
                returncode=0, stdout=json.dumps({"manual_pairing_code": "ZZZZ-9999"}), stderr=""
            )
        }
    )
    with pytest.raises(ProtocolError):
        await adapter(runner=runner).pair()


async def test_a_failed_pair_raises_without_echoing_what_codex_printed() -> None:
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["pair"]: CommandResult(
                returncode=1, stdout="", stderr="not logged in: /home/user/.codex/auth.json"
            )
        }
    )
    with pytest.raises(ProtocolError) as raised:
        await adapter(runner=runner).pair()
    assert "auth.json" not in str(raised.value)


# -------------------------------------------------------------------------------- timeouts


async def test_every_runner_call_is_bounded_by_a_timeout_no_longer_than_thirty_seconds() -> None:
    """A `codex` command that never returns must not hold the operation lock forever."""
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["enable"]: CommandResult(
                returncode=0, stdout=fixture("start-connecting.json"), stderr=""
            ),
            REMOTE_CONTROL_ARGV["pair"]: CommandResult(
                returncode=0, stdout=fixture("pair.json"), stderr=""
            ),
            REMOTE_CONTROL_ARGV["daemon_probe"]: CommandResult(
                returncode=1, stdout="", stderr="failed to connect to app-server-control.sock"
            ),
        }
    )
    rpc = FakeRpc(error=ProtocolError("unavailable"))
    subject = adapter(runner=runner, rpc=rpc)

    await subject.set_state(RemoteControlState.ACTIVE)
    await subject.set_state(RemoteControlState.INACTIVE)
    await subject.pair()
    await subject.status()

    assert runner.calls, "a test that recorded no calls would pass vacuously"
    for argv, timeout in runner.calls:
        assert 0 < timeout <= 30, (argv, timeout)


# -------------------------------------------------------------------------------- lifecycle


async def test_closing_the_adapter_closes_the_proxy_session_it_opened() -> None:
    """`status()` spawns a long-lived `codex app-server proxy` child on first use."""

    class ClosableRpc(FakeRpc):
        closed: bool = False

        async def close(self) -> None:
            self.closed = True

    rpc = ClosableRpc(payload={"status": "connected"})
    subject = adapter(rpc=rpc)
    await subject.status()
    await subject.aclose()

    assert rpc.closed is True


async def test_closing_an_adapter_whose_client_cannot_close_is_not_an_error() -> None:
    """A test double should not have to grow a lifecycle to be usable."""
    await adapter().aclose()
