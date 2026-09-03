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


#: What `codex app-server daemon version` prints when nothing is listening, reproduced from
#: a real run on a daemon-less host: the socket path AND the ENOENT cause.
ABSENT_PROBE = CommandResult(
    returncode=1,
    stdout="",
    stderr=(
        "Error: failed to connect to "
        "/home/user/.codex/app-server-control/app-server-control.sock\n\n"
        "Caused by:\n    No such file or directory (os error 2)"
    ),
)

#: A daemon that is up: the probe answers.
RUNNING_PROBE = CommandResult(returncode=0, stdout='{"cli":"0.151.0"}', stderr="")


# --------------------------------------------------------------------------- the argv table


def test_the_argv_table_is_a_closed_module_constant_that_only_runs_codex() -> None:
    assert REMOTE_CONTROL_ARGV, "an empty table would pass every assertion below vacuously"
    for name, argv in REMOTE_CONTROL_ARGV.items():
        assert isinstance(argv, tuple), name
        assert argv[0] == "codex", name


def test_the_table_is_pinned_to_exactly_these_six_vectors() -> None:
    """An exact pin, because a predicate can only forbid the danger somebody thought of.

    The first version of this test asserted `"stop" not in argv`, which reads as "no teardown
    verb" and actually means "not that one spelling". `terminate`, `kill`, `shutdown` and
    `reset` would all have passed it, and so would any *other* dangerous vector that simply
    is not a teardown -- nothing else here bounds the table's keys at all.

    So the guard is the table itself. Adding, removing or repointing any vector fails here
    and forces a reviewed edit, which is the discipline `test_descriptor_fields_are_pinned.py`
    already applies one layer up. The banned-verb check below is kept as well: the pin says
    what the table IS, and that says what it must never become, which is the sentence a
    reader reaches for when they are about to add a sixth entry.
    """
    assert dict(REMOTE_CONTROL_ARGV) == {
        "status": ("codex", "app-server", "proxy"),
        "daemon_probe": ("codex", "app-server", "daemon", "version"),
        "enable_when_absent": ("codex", "remote-control", "start", "--json"),
        "enable_when_running": ("codex", "app-server", "daemon", "enable-remote-control"),
        "disable": ("codex", "app-server", "daemon", "disable-remote-control"),
        "pair": ("codex", "remote-control", "pair", "--json"),
    }


#: Every spelling by which a `codex` invocation could take the shared daemon down, and with
#: it every TUI pane attached to that daemon. Broader than the one verb the CLI uses today,
#: because the cost of guessing wrong is the owner's live agent sessions.
TEARDOWN_VERBS = ("stop", "terminate", "kill", "shutdown", "restart", "reset", "down")


def test_no_entry_in_the_table_can_tear_the_daemon_down() -> None:
    """Kept beside the pin: the pin says what the table is, this says what it must not be."""
    for name, argv in REMOTE_CONTROL_ARGV.items():
        for verb in TEARDOWN_VERBS:
            assert verb not in argv, f"{name} carries the teardown verb {verb!r}"


def test_the_teardown_guard_can_actually_catch_one() -> None:
    """A guard that matched nothing would pass for the wrong reason."""
    assert any(verb in ("codex", "app-server", "daemon", "stop") for verb in TEARDOWN_VERBS), (
        "the banned-verb list no longer matches the CLI's own teardown vector"
    )


def test_the_table_cannot_be_repointed_at_runtime() -> None:
    """`closed by construction` is a claim about the object, not about the author."""
    import pytest as _pytest

    with _pytest.raises(TypeError):
        REMOTE_CONTROL_ARGV["disable"] = ("codex", "app-server", "daemon", "stop")  # type: ignore[index]


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
    runner = FakeRunner(results={REMOTE_CONTROL_ARGV["daemon_probe"]: ABSENT_PROBE})
    rpc = FakeRpc(error=ProtocolError("provider protocol is unavailable"))

    result = await adapter(runner=runner, rpc=rpc).status()

    assert result.connection is HostConnection.DAEMON_ABSENT
    assert result.state is RemoteControlState.UNKNOWN, (
        "the enrollment preference outlives the daemon, so a stopped daemon is not 'off'"
    )
    assert result.server_name is None


async def test_a_socket_we_cannot_reach_is_not_a_socket_that_is_not_there() -> None:
    """The dangerous confusion: both failures name the socket path.

    A daemon owned by another uid, a divergent CODEX_HOME between the interactive shell and
    the user service, or a backlog refusal all print the same "failed to connect to <path>"
    line as a missing socket. Reading those as DAEMON_ABSENT would report a host that IS
    remote-controlled as one that is not.
    """
    for cause in ("Permission denied (os error 13)", "Connection refused (os error 111)"):
        runner = FakeRunner(
            results={
                REMOTE_CONTROL_ARGV["daemon_probe"]: CommandResult(
                    returncode=1,
                    stdout="",
                    stderr=(
                        "Error: failed to connect to "
                        "/home/user/.codex/app-server-control/app-server-control.sock\n\n"
                        f"Caused by:\n    {cause}"
                    ),
                )
            }
        )
        rpc = FakeRpc(error=ProtocolError("provider protocol is unavailable"))

        with pytest.raises(ProtocolError):
            await adapter(runner=runner, rpc=rpc).status()


async def test_any_other_rpc_failure_is_raised_rather_than_read_as_no_daemon() -> None:
    """A daemon that is up and failing must not render as a daemon that is not there."""
    runner = FakeRunner(results={REMOTE_CONTROL_ARGV["daemon_probe"]: RUNNING_PROBE})
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


async def test_turning_it_on_with_no_daemon_running_starts_one() -> None:
    """Nothing is listening, so the bootstrap `start` may perform has nothing to stop."""
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["daemon_probe"]: ABSENT_PROBE,
            REMOTE_CONTROL_ARGV["enable_when_absent"]: CommandResult(
                returncode=0, stdout=fixture("start-connected.json"), stderr=""
            ),
        }
    )
    rpc = FakeRpc(payload={"status": "disabled"})
    result = await adapter(runner=runner, rpc=rpc).set_state(RemoteControlState.ACTIVE)

    assert runner.argvs == [
        ("codex", "app-server", "daemon", "version"),
        ("codex", "remote-control", "start", "--json"),
    ]
    assert result.connection is HostConnection.CONNECTED
    assert result.server_name == "Paisleys-Blender"
    assert rpc.calls == [], "a settled connected envelope needs no second opinion"


async def test_turning_it_on_with_a_daemon_already_up_never_runs_start() -> None:
    """The whole point of the two-verb rule.

    `remote-control start` reaches `bootstrap_locked` on a host that is not bootstrapped,
    and that function stops a running managed backend before starting its own -- every pane
    attached to it exits on disconnect. The daemon-scoped verb flips the preference on the
    live daemon and starts nothing, so the destructive branch is not merely unused here, it
    is unreachable: the only state in which it could stop something is the state in which
    this path does not call it.
    """
    runner = FakeRunner(results={REMOTE_CONTROL_ARGV["daemon_probe"]: RUNNING_PROBE})
    rpc = FakeRpc(payload={"status": "connected", "serverName": "Paisleys-Blender"})

    result = await adapter(runner=runner, rpc=rpc).set_state(RemoteControlState.ACTIVE)

    assert runner.argvs == [
        ("codex", "app-server", "daemon", "version"),
        ("codex", "app-server", "daemon", "enable-remote-control"),
    ]
    assert ("codex", "remote-control", "start", "--json") not in runner.argvs
    assert result.connection is HostConnection.CONNECTED


async def test_the_daemon_scoped_verb_is_answered_by_re_reading_the_daemon() -> None:
    """It prints human-readable text only, so the re-read is the whole answer."""
    runner = FakeRunner(results={REMOTE_CONTROL_ARGV["daemon_probe"]: RUNNING_PROBE})
    rpc = FakeRpc(payload={"status": "connecting", "serverName": "Paisleys-Blender"})

    result = await adapter(runner=runner, rpc=rpc).set_state(RemoteControlState.ACTIVE)

    assert rpc.calls == [("remoteControl/status/read", {})]
    assert result.connection is HostConnection.CONNECTING


async def test_an_enable_that_timed_out_connecting_asks_the_daemon_instead() -> None:
    """`timedOut` means "enrolled, but the relay link did not come up while we waited"."""
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["daemon_probe"]: ABSENT_PROBE,
            REMOTE_CONTROL_ARGV["enable_when_absent"]: CommandResult(
                returncode=0, stdout=fixture("start-connecting.json"), stderr=""
            ),
        }
    )
    rpc = FakeRpc(payload={"status": "connected", "serverName": "Paisleys-Blender"})

    result = await adapter(runner=runner, rpc=rpc).set_state(RemoteControlState.ACTIVE)

    assert ("remoteControl/status/read", {}) in rpc.calls
    assert result.connection is HostConnection.CONNECTED, "the daemon is the authority"


async def test_a_bailed_enable_reports_what_the_daemon_says_not_a_flat_error() -> None:
    """`disabled` and `errored` bail with a non-zero exit and NO json on stdout."""
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["daemon_probe"]: RUNNING_PROBE,
            REMOTE_CONTROL_ARGV["enable_when_running"]: CommandResult(
                returncode=1, stdout="", stderr="Remote control is disabled on Paisleys-Blender"
            ),
        }
    )
    rpc = FakeRpc(payload={"status": "disabled", "serverName": "Paisleys-Blender"})

    result = await adapter(runner=runner, rpc=rpc).set_state(RemoteControlState.ACTIVE)

    assert result.connection is HostConnection.DISABLED
    assert result.state is RemoteControlState.INACTIVE


async def test_an_absent_daemon_is_not_believed_immediately_after_an_enable() -> None:
    """Reporting "not reachable" for a machine we just enrolled is the wrong direction."""
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["daemon_probe"]: ABSENT_PROBE,
            REMOTE_CONTROL_ARGV["enable_when_absent"]: CommandResult(
                returncode=0, stdout=fixture("start-connecting.json"), stderr=""
            ),
        }
    )
    rpc = FakeRpc(error=ProtocolError("provider protocol is unavailable"))

    result = await adapter(runner=runner, rpc=rpc).set_state(RemoteControlState.ACTIVE)

    assert result.connection is HostConnection.CONNECTING
    assert result.state is RemoteControlState.ACTIVE


async def test_a_failed_enable_is_errored_and_does_not_echo_what_codex_printed() -> None:
    """Provider stderr is rendered by nothing here: it can carry a path, a token, a prompt."""
    secret = "token=sk-abcdef0123456789"
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["daemon_probe"]: RUNNING_PROBE,
            REMOTE_CONTROL_ARGV["enable_when_running"]: CommandResult(
                returncode=1, stdout="", stderr=f"auth failed: {secret}"
            ),
        }
    )
    result = await adapter(runner=runner).set_state(RemoteControlState.ACTIVE)

    assert result == HostRemoteControlStatus.observed(HostConnection.ERRORED, server_name=None)
    assert secret not in repr(result)
    assert secret not in str(result)


async def test_an_enable_whose_json_is_unreadable_is_errored_and_echoes_nothing() -> None:
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["daemon_probe"]: ABSENT_PROBE,
            REMOTE_CONTROL_ARGV["enable_when_absent"]: CommandResult(
                returncode=0, stdout="not json at all: /home/user/secret", stderr=""
            ),
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
    """`pairingCode` is the app-to-app handshake; only the manual code is typed by a human."""
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["pair"]: CommandResult(
                returncode=0,
                stdout=json.dumps(
                    {
                        "pairingCode": "0000-0000",
                        "manualPairingCode": "ZZZZ-9999",
                        "expiresAt": 1788436800,
                    }
                ),
                stderr="",
            )
        }
    )
    assert (await adapter(runner=runner).pair()).code == "ZZZZ-9999"


async def test_the_snake_case_spelling_is_still_accepted() -> None:
    """The relay wire protocol spells it snake_case and never reaches stdout -- but Codex's
    JSON is convention rather than contract (DEC-063), and tolerating it costs one `or`."""
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["pair"]: CommandResult(
                returncode=0,
                stdout=json.dumps({"manual_pairing_code": "ZZZZ-9999", "expires_at": 1788436800}),
                stderr="",
            )
        }
    )
    assert (await adapter(runner=runner).pair()).code == "ZZZZ-9999"


async def test_an_enrollment_offering_no_manual_code_fails_closed() -> None:
    """`manualPairingCode` is nullable upstream. A null must not render as an empty box."""
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["pair"]: CommandResult(
                returncode=0, stdout=fixture("pair-no-manual-code.json"), stderr=""
            )
        }
    )
    with pytest.raises(ProtocolError):
        await adapter(runner=runner).pair()


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
            REMOTE_CONTROL_ARGV["daemon_probe"]: ABSENT_PROBE,
            REMOTE_CONTROL_ARGV["enable_when_absent"]: CommandResult(
                returncode=0, stdout=fixture("start-connecting.json"), stderr=""
            ),
            REMOTE_CONTROL_ARGV["pair"]: CommandResult(
                returncode=0, stdout=fixture("pair.json"), stderr=""
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
    assert {argv for argv, _ in runner.calls} >= {
        REMOTE_CONTROL_ARGV["daemon_probe"],
        REMOTE_CONTROL_ARGV["enable_when_absent"],
        REMOTE_CONTROL_ARGV["disable"],
        REMOTE_CONTROL_ARGV["pair"],
    }, "the sweep must actually reach every runner-backed verb"
    for argv, timeout in runner.calls:
        assert 0 < timeout <= 30, (argv, timeout)


async def test_a_hostile_pairing_code_is_refused_rather_than_cleaned() -> None:
    """A scrubbed secret is a secret that no longer works, shown to someone who would type it."""
    for hostile in (
        "\x1b]0;pwned\x07ZZZZ-9999",
        "A" * 200,
        "ZZZZ\u202e9999",
        "ZZZZ\u200b9999",
        "  ZZZZ-9999  ",
    ):
        runner = FakeRunner(
            results={
                REMOTE_CONTROL_ARGV["pair"]: CommandResult(
                    returncode=0,
                    stdout=json.dumps({"manualPairingCode": hostile, "expiresAt": 1788436800}),
                    stderr="",
                )
            }
        )
        with pytest.raises(ProtocolError):
            await adapter(runner=runner).pair()


async def test_an_absurd_expiry_is_refused_in_the_boundary_s_own_vocabulary() -> None:
    """NaN, 1e30 and 10**30 each raise a different arithmetic type out of the stdlib."""
    for absurd in (float("nan"), 1e30, -1e30, 10**30, True):
        runner = FakeRunner(
            results={
                REMOTE_CONTROL_ARGV["pair"]: CommandResult(
                    returncode=0,
                    stdout=json.dumps({"manualPairingCode": "ZZZZ-9999", "expiresAt": absurd}),
                    stderr="",
                )
            }
        )
        with pytest.raises(ProtocolError):
            await adapter(runner=runner).pair()


def test_the_command_result_does_not_print_what_codex_wrote() -> None:
    """It carries the pairing code on its way to `PairingCode`; tracebacks render locals."""
    rendered = repr(CommandResult(returncode=0, stdout="ZZZZ-9999", stderr="/home/user/secret"))
    assert "ZZZZ-9999" not in rendered
    assert "secret" not in rendered
    assert "returncode=0" in rendered


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


async def test_an_install_that_cannot_run_a_daemon_reads_as_unreachable() -> None:
    """The daemon surface needs OpenAI's standalone codex; the npm one answers with this.

    Found by running the live drill on a host with the npm distribution. Every daemon verb --
    not only the enable -- refuses, so there is no daemon and cannot be one. Mapping it to
    ERRORED, which is where it landed first, told the owner the daemon reported its own link
    broken: a machine with no link to break, and no way to learn why the button did nothing.
    """
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["daemon_probe"]: ABSENT_PROBE,
            REMOTE_CONTROL_ARGV["enable_when_absent"]: CommandResult(
                returncode=1,
                stdout="",
                stderr=(
                    "Error: managed standalone Codex install not found at "
                    "/home/user/.codex/packages/standalone/current/codex\n\n"
                    "This command requires the standalone install managed by the Codex "
                    "installer, because the daemon starts and updates app-server from that "
                    "fixed path."
                ),
            ),
        }
    )
    result = await adapter(runner=runner).set_state(RemoteControlState.ACTIVE)

    assert result.connection is HostConnection.UNREACHABLE
    assert result.connection is not HostConnection.ERRORED
    assert result.state is RemoteControlState.UNKNOWN
    # And still nothing of what codex printed.
    assert "/home/user" not in repr(result)
    assert "standalone" not in repr(result)


async def test_the_disable_verb_also_classifies_an_install_that_cannot_serve_a_daemon() -> None:
    """Measured not to happen on the npm build, where `disable-remote-control` succeeds.

    Pinned anyway, because the classification belongs to the message rather than to the branch
    it was first seen in: the first version of this correction reached only `_enable`, and a
    close-out review caught the asymmetry before anything shipped.
    """
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["disable"]: CommandResult(
                returncode=1,
                stdout="",
                stderr="Error: managed standalone Codex install not found at /x/codex",
            )
        }
    )
    rpc = FakeRpc(payload={"status": "connected"})
    result = await adapter(runner=runner, rpc=rpc).set_state(RemoteControlState.INACTIVE)

    assert result.connection is HostConnection.UNREACHABLE
    assert rpc.calls == [], "an install that cannot serve a daemon has nothing to re-read"


def test_both_directions_share_one_classifier_for_that_message() -> None:
    """One predicate, so the two verbs cannot drift about what the same message means."""
    from remote_agents.adapters.agents.codex.remote_control import _cannot_start_a_daemon

    unsupported = CommandResult(
        returncode=1, stdout="", stderr="managed standalone Codex install not found at /x"
    )
    assert _cannot_start_a_daemon(unsupported) is True
    assert _cannot_start_a_daemon(CommandResult(returncode=1, stdout="", stderr="boom")) is False
