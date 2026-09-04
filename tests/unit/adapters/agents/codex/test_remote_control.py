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
    CodexHomeSettings,
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
class FakeSettings:
    """The persisted preference, scripted three-valued, counting how often it was read.

    `None` is the interesting value and the reason this is not a plain bool: it means the
    file could not be read, which must never become "off".
    """

    preference: bool | None = False
    reads: int = 0

    async def remote_control_preference(self) -> bool | None:
        self.reads += 1
        return self.preference


def adapter(
    runner: FakeRunner | None = None, settings: FakeSettings | None = None
) -> CodexRemoteControl:
    return CodexRemoteControl(runner=runner or FakeRunner(), settings=settings or FakeSettings())


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


def test_the_table_is_pinned_to_exactly_these_five_vectors() -> None:
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

    Five since 2026-09-03: `status` -> `codex app-server proxy` was removed with the read
    path that used it. It named a transport that never answered `initialize` on any host
    this was run against, for a method the protocol does not define (BL-040).
    """
    assert dict(REMOTE_CONTROL_ARGV) == {
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


async def test_status_reads_the_preference_then_whether_anything_is_serving_it() -> None:
    """The whole read, in the order it happens: one file, then one probe that starts nothing."""
    settings = FakeSettings(preference=True)
    runner = FakeRunner(results={REMOTE_CONTROL_ARGV["daemon_probe"]: RUNNING_PROBE})

    result = await adapter(runner=runner, settings=settings).status()

    assert settings.reads == 1
    assert runner.argvs == [("codex", "app-server", "daemon", "version")]
    assert result.connection is HostConnection.CONNECTED
    assert result.state is RemoteControlState.ACTIVE


async def test_the_preference_being_off_is_the_whole_answer() -> None:
    """A running daemon with the preference off is genuinely off; the probe adds nothing.

    Asserted as "the probe did not run" rather than just "the reading is DISABLED", because
    the point is that the file is authoritative for *off* -- a later edit that consulted the
    daemon first would still produce DISABLED here and would have changed what the reading
    means.
    """
    runner = FakeRunner(results={REMOTE_CONTROL_ARGV["daemon_probe"]: RUNNING_PROBE})

    result = await adapter(runner=runner, settings=FakeSettings(preference=False)).status()

    assert result.connection is HostConnection.DISABLED
    assert result.state is RemoteControlState.INACTIVE
    assert runner.argvs == [], "off needs no second opinion from a daemon"


async def test_enabled_with_nothing_listening_is_not_reported_as_off() -> None:
    """The reason DAEMON_ABSENT exists: the preference outlives the process serving it."""
    runner = FakeRunner(results={REMOTE_CONTROL_ARGV["daemon_probe"]: ABSENT_PROBE})

    result = await adapter(runner=runner, settings=FakeSettings(preference=True)).status()

    assert result.connection is HostConnection.DAEMON_ABSENT
    assert result.state is RemoteControlState.UNKNOWN
    assert result.state is not RemoteControlState.INACTIVE, (
        "one daemon start away from reachable is not the same as not enrolled"
    )


async def test_a_socket_we_cannot_reach_is_not_a_socket_that_is_not_there() -> None:
    """The dangerous confusion: both failures name the socket path.

    A daemon owned by another uid, a divergent CODEX_HOME between the interactive shell and
    the user service, or a backlog refusal all print the same "failed to connect to <path>"
    line as a missing socket. Reading those as DAEMON_ABSENT would assert something definite
    about a host we could not question.
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

        result = await adapter(runner=runner, settings=FakeSettings(preference=True)).status()

        assert result.connection is HostConnection.UNREACHABLE, cause
        assert result.connection is not HostConnection.DAEMON_ABSENT, cause


async def test_an_unreadable_preference_is_never_read_as_off() -> None:
    """The fail-closed rule, at the one boundary where breaking it is silent.

    A future Codex that renames or moves the settings file makes every read `None`. If that
    became DISABLED, every surface would confidently report "off" for a machine still enrolled
    with the relay -- the owner's phone would keep working while the terminal said it could
    not. UNKNOWN is the honest word and the one the surfaces already render.
    """
    runner = FakeRunner(results={REMOTE_CONTROL_ARGV["daemon_probe"]: RUNNING_PROBE})

    result = await adapter(runner=runner, settings=FakeSettings(preference=None)).status()

    assert result.connection is HostConnection.UNREACHABLE
    assert result.state is RemoteControlState.UNKNOWN
    assert result.state is not RemoteControlState.INACTIVE


async def test_codex_being_absent_is_a_reading_rather_than_a_raised_error() -> None:
    """A surface drawing a row cannot render an exception; it can render "no answer"."""

    class MissingCodex:
        async def run(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult:
            raise ProtocolError("codex is not installed on this host")

    subject = CodexRemoteControl(runner=MissingCodex(), settings=FakeSettings(preference=True))

    assert (await subject.status()).connection is HostConnection.UNREACHABLE


async def test_reading_the_status_never_runs_a_command_that_changes_anything() -> None:
    """`status()` is a read. The probe starts no daemon; nothing else is invoked at all."""
    runner = FakeRunner(results={REMOTE_CONTROL_ARGV["daemon_probe"]: RUNNING_PROBE})

    await adapter(runner=runner, settings=FakeSettings(preference=True)).status()

    for name in ("enable_when_absent", "enable_when_running", "disable", "pair"):
        assert REMOTE_CONTROL_ARGV[name] not in runner.argvs, name


# ------------------------------------------------------- the preference file itself


@pytest.mark.parametrize(
    ("label", "content", "expected"),
    [
        ("missing file", None, None),
        ("not json", "}{ nonsense", None),
        ("not an object", "[1, 2, 3]", None),
        ("key absent", '{"somethingElse": true}', None),
        # `isinstance(True, int)` is True in Python, so a numeric 1 would read as enabled
        # under any check looser than `isinstance(value, bool)`.
        ("numeric one", '{"remoteControlEnabled": 1}', None),
        ("string true", '{"remoteControlEnabled": "true"}', None),
        ("null", '{"remoteControlEnabled": null}', None),
        ("genuinely false", '{"remoteControlEnabled": false}', False),
        ("genuinely true", '{"remoteControlEnabled": true}', True),
    ],
)
async def test_only_a_real_boolean_is_an_answer(
    label: str, content: str | None, expected: bool | None, tmp_path: Path
) -> None:
    """Everything else is `None`, which the reading above turns into UNKNOWN, never "off"."""
    home = tmp_path / label.replace(" ", "_")
    (home / "app-server-daemon").mkdir(parents=True)
    if content is not None:
        (home / "app-server-daemon" / "settings.json").write_text(content, encoding="utf-8")

    assert await CodexHomeSettings(home=home).remote_control_preference() is expected


async def test_a_settings_file_too_large_to_be_one_is_refused_unread(tmp_path: Path) -> None:
    """A `CODEX_HOME` pointed somewhere wrong should not read a huge file to answer a toggle."""
    home = tmp_path / "huge"
    (home / "app-server-daemon").mkdir(parents=True)
    path = home / "app-server-daemon" / "settings.json"
    path.write_text(" " * (64 * 1024 + 1) + '{"remoteControlEnabled": true}', encoding="utf-8")

    assert await CodexHomeSettings(home=home).remote_control_preference() is None


async def test_the_reader_follows_codex_home_rather_than_assuming_a_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`codex` honours CODEX_HOME -- verified live by watching its control socket move.

    A reader that always looked in `~/.codex` would, on a host whose user service and
    interactive shell disagree about CODEX_HOME, report the wrong machine's preference with
    complete confidence.
    """
    home = tmp_path / "elsewhere"
    (home / "app-server-daemon").mkdir(parents=True)
    (home / "app-server-daemon" / "settings.json").write_text(
        '{"remoteControlEnabled": true}', encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(home))

    assert await CodexHomeSettings().remote_control_preference() is True

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nowhere"))
    assert await CodexHomeSettings().remote_control_preference() is None


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
    settings = FakeSettings(preference=False)
    result = await adapter(runner=runner, settings=settings).set_state(RemoteControlState.ACTIVE)

    assert runner.argvs == [
        ("codex", "app-server", "daemon", "version"),
        ("codex", "remote-control", "start", "--json"),
    ]
    assert result.connection is HostConnection.CONNECTED
    assert result.server_name == "Paisleys-Blender"
    assert settings.reads == 0, "a settled connected envelope needs no second opinion"


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

    result = await adapter(runner=runner, settings=FakeSettings(preference=True)).set_state(
        RemoteControlState.ACTIVE
    )

    # Probe, the daemon-scoped verb, then the re-read's own probe. The third entry is not
    # slack: the verb reports the preference it wrote, not whether anything serves it.
    assert runner.argvs == [
        ("codex", "app-server", "daemon", "version"),
        ("codex", "app-server", "daemon", "enable-remote-control"),
        ("codex", "app-server", "daemon", "version"),
    ]
    assert ("codex", "remote-control", "start", "--json") not in runner.argvs
    assert result.connection is HostConnection.CONNECTED


async def test_the_daemon_scoped_verb_is_answered_by_re_reading_the_daemon() -> None:
    """What it reports is the preference it wrote, so the re-read is the whole answer.

    (It does print JSON -- an earlier version of this docstring said "human-readable text
    only", which was wrong -- but about the preference, not about what is serving it.)
    """
    runner = FakeRunner(results={REMOTE_CONTROL_ARGV["daemon_probe"]: RUNNING_PROBE})
    settings = FakeSettings(preference=True)

    result = await adapter(runner=runner, settings=settings).set_state(RemoteControlState.ACTIVE)

    assert settings.reads == 1
    assert result.connection is HostConnection.CONNECTED


async def test_an_enable_that_timed_out_connecting_asks_the_daemon_instead() -> None:
    """`timedOut` means "enrolled, but the relay link did not come up while we waited"."""
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["daemon_probe"]: RUNNING_PROBE,
            REMOTE_CONTROL_ARGV["enable_when_running"]: CommandResult(
                returncode=0, stdout=fixture("start-connecting.json"), stderr=""
            ),
        }
    )
    settings = FakeSettings(preference=True)

    result = await adapter(runner=runner, settings=settings).set_state(RemoteControlState.ACTIVE)

    assert settings.reads == 1, "an unsettled envelope is not the final word"
    assert result.connection is HostConnection.CONNECTED, "the host's own state is the authority"


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
    result = await adapter(runner=runner, settings=FakeSettings(preference=False)).set_state(
        RemoteControlState.ACTIVE
    )

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
    result = await adapter(runner=runner, settings=FakeSettings(preference=True)).set_state(
        RemoteControlState.ACTIVE
    )

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
    # An unreadable preference, so the re-read has no opinion and the terminal ERRORED
    # fallback is what is under test. With a *readable* preference a failed enable reports
    # that preference instead -- a strictly better answer, pinned by
    # `test_a_bailed_enable_reports_what_the_daemon_says_not_a_flat_error` above.
    result = await adapter(runner=runner, settings=FakeSettings(preference=None)).set_state(
        RemoteControlState.ACTIVE
    )

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
    result = await adapter(runner=runner, settings=FakeSettings(preference=None)).set_state(
        RemoteControlState.ACTIVE
    )

    assert result.connection is HostConnection.ERRORED
    assert "secret" not in repr(result)


async def test_turning_it_off_disables_the_preference_and_re_reads_the_status() -> None:
    """The verb writes the preference; the reading that follows is read back, not assumed.

    This docstring used to say "`off` never tears the daemon down". It does restart it --
    measured -- which costs an attached pane its conversation but not its life. The re-read
    is the part this test is about, and it is why the surfaces report what the host now says
    rather than what the command claimed.
    """
    runner = FakeRunner()
    settings = FakeSettings(preference=False)

    result = await adapter(runner=runner, settings=settings).set_state(RemoteControlState.INACTIVE)

    assert runner.argvs == [("codex", "app-server", "daemon", "disable-remote-control")]
    assert settings.reads == 1
    assert result.connection is HostConnection.DISABLED


async def test_a_failed_disable_is_errored_and_does_not_re_read() -> None:
    runner = FakeRunner(
        results={
            REMOTE_CONTROL_ARGV["disable"]: CommandResult(
                returncode=1, stdout="", stderr="no such daemon /tmp/private"
            )
        }
    )
    settings = FakeSettings(preference=True)

    result = await adapter(runner=runner, settings=settings).set_state(RemoteControlState.INACTIVE)

    assert result == HostRemoteControlStatus.observed(HostConnection.ERRORED, server_name=None)
    assert settings.reads == 0, "a failed disable must not be reported as a re-read reading"
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
    subject = adapter(runner=runner, settings=FakeSettings(preference=True))

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


async def test_closing_the_adapter_closes_a_collaborator_that_holds_something_open() -> None:
    """Nothing holds a resource today, so this pins the *hook* rather than a live child.

    It read "closes the proxy session it opened" until 2026-09-03, when the long-lived
    `codex app-server proxy` child was removed with the dead read path. The hook stays wired
    and every caller still awaits it, so a collaborator that grows a resource later is closed
    without anyone having to remember to re-add the plumbing -- which is exactly what this
    asserts, using a double that does hold one.
    """

    class ClosableSettings(FakeSettings):
        closed: bool = False

        async def close(self) -> None:
            self.closed = True

    settings = ClosableSettings(preference=True)
    subject = adapter(settings=settings)
    await subject.status()
    await subject.aclose()

    assert settings.closed is True


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
    settings = FakeSettings(preference=True)
    result = await adapter(runner=runner, settings=settings).set_state(RemoteControlState.INACTIVE)

    assert result.connection is HostConnection.UNREACHABLE
    assert settings.reads == 0, "an install that cannot serve a daemon has nothing to re-read"


def test_both_directions_share_one_classifier_for_that_message() -> None:
    """One predicate, so the two verbs cannot drift about what the same message means."""
    from remote_agents.adapters.agents.codex.remote_control import _cannot_start_a_daemon

    unsupported = CommandResult(
        returncode=1, stdout="", stderr="managed standalone Codex install not found at /x"
    )
    assert _cannot_start_a_daemon(unsupported) is True
    assert _cannot_start_a_daemon(CommandResult(returncode=1, stdout="", stderr="boom")) is False
