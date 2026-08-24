"""`doctor` asks the supervisor port whether the service is up, and says which one it asked.

Before this, the liveness probe was a `systemctl` argv spelled out inline in `bootstrap`, so
on a Mac `doctor` reported the service inactive by running a command that is not installed --
a false negative indistinguishable from a stopped service. Routing it through the port makes
the question platform-neutral and the answer attributable.

**Nothing here supplies `launchctl print` output for anything to parse**, and that absence is
the test. `launchctl(1)` documents that output as not API ("Do NOT rely on the structure"), so
liveness is the exit status and only the exit status; a fixture carrying fake `print` text
would be the first step toward code that reads it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from remote_agents.adapters.supervisor.launchd import LaunchdSupervisor
from remote_agents.adapters.supervisor.systemd import SystemdSupervisor
from remote_agents.bootstrap import main
from remote_agents.domain.models import ProfileId
from remote_agents.domain.profiles import ProfileCompatibility
from remote_agents.ports.service_supervisor import LivenessMeaning, SupervisorKind


class _DoctorPaths:
    """The `ProductionPaths` stand-in, carrying a real 0600 credential file `doctor` parses."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.home = config_path.parent
        self.environment_path = config_path.parent / "telegram.env"
        self.environment_path.write_text(
            "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=test-token\n"
            "REMOTE_AGENTS_OWNER_USER_ID=7\n"
            "REMOTE_AGENTS_OWNER_CHAT_ID=11\n",
            encoding="utf-8",
        )
        self.environment_path.chmod(0o600)

    def require_private_environment(self) -> Path:
        return self.environment_path


def _compatibility(profile_id: str) -> ProfileCompatibility:
    return ProfileCompatibility(ProfileId(profile_id), True, "1.2.3", "AVAILABLE", None)


def _arrange(tmp_path, monkeypatch, supervisor, *, liveness_exit_zero: bool) -> list[tuple]:
    """Wire `doctor` over one injected supervisor, faking only the *exit status* of its probe.

    Returns the argv list every `_command_succeeds` call was made with, so a test can assert
    which command was actually run rather than trusting that the right one was chosen.
    """
    config = tmp_path / "config.toml"
    config.write_text(
        "[paths]\n"
        f'dev_root = "{tmp_path}"\n'
        f'registry_path = "{tmp_path / "registry.yaml"}"\n'
        f'database_path = "{tmp_path / "sessions.sqlite3"}"\n\n'
        "[limits]\nmax_label_length = 40\nproject_page_size = 10\n"
        "activity_poll_seconds = 30\nactivity_quiet_polls = 3\n",
        encoding="utf-8",
    )
    paths = _DoctorPaths(config)
    invoked: list[tuple] = []

    def _fake_command_succeeds(argv: tuple[str, ...]) -> bool:
        invoked.append(tuple(argv))
        # Keyed on the argv rather than answering everything the same way: tmux's probe goes
        # through this helper too, so a blanket False would fail a component this test says
        # nothing about and make the assertion about `service` unreadable.
        if tuple(argv) == tuple(supervisor.liveness_command()):
            return liveness_exit_zero
        return True

    monkeypatch.setattr("remote_agents.bootstrap.ProductionPaths.for_home", lambda _home: paths)
    monkeypatch.setattr("remote_agents.bootstrap.database_is_ready", lambda _path: True)
    monkeypatch.setattr("remote_agents.bootstrap._command_succeeds", _fake_command_succeeds)
    monkeypatch.setattr("remote_agents.bootstrap._supervisor_for_host", lambda: supervisor)
    monkeypatch.setattr(
        "remote_agents.bootstrap._telegram_credentials_are_private", lambda _paths: True
    )
    monkeypatch.setattr(
        "remote_agents.bootstrap.load_registry",
        lambda _path: SimpleNamespace(projects=(), error=None),
    )
    monkeypatch.setattr("remote_agents.bootstrap.discover_projects", lambda _path: ())
    monkeypatch.setattr("remote_agents.bootstrap.build_catalogue", lambda *_a, **_k: ())
    monkeypatch.setattr(
        "remote_agents.bootstrap.probe_profiles",
        lambda *_a, **_k: tuple(
            _compatibility(name)
            for name in ("claude", "claude-remote", "codex", "opencode", "cursor-agent")
        ),
    )
    return invoked


_SUPERVISORS = (
    pytest.param(
        SystemdSupervisor(interpreter=Path("/opt/ra/bin/python3"), home=Path("/home/tester")),
        SupervisorKind.SYSTEMD,
        id="systemd",
    ),
    pytest.param(
        LaunchdSupervisor(
            interpreter=Path("/opt/ra/bin/python3"),
            home=Path("/Users/tester"),
            uid=501,
            homebrew_prefix=lambda: None,
        ),
        SupervisorKind.LAUNCHD,
        id="launchd",
    ),
)


@pytest.mark.parametrize(("supervisor", "kind"), _SUPERVISORS)
def test_doctor_reports_the_service_up_when_the_probe_exits_zero(
    tmp_path, monkeypatch, capsys, supervisor, kind
) -> None:
    """Zero exit means up, on either supervisor, with no output consulted to decide it."""
    invoked = _arrange(tmp_path, monkeypatch, supervisor, liveness_exit_zero=True)

    assert main(["doctor", "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["components"]["service"] == {"status": "healthy", "reason": None}
    assert tuple(supervisor.liveness_command()) in invoked


@pytest.mark.parametrize(("supervisor", "kind"), _SUPERVISORS)
def test_doctor_reports_the_service_down_when_the_probe_exits_non_zero(
    tmp_path, monkeypatch, capsys, supervisor, kind
) -> None:
    """A non-zero exit is the whole signal -- the same one systemd and launchd both give."""
    invoked = _arrange(tmp_path, monkeypatch, supervisor, liveness_exit_zero=False)

    # Exit stays 0: on the config-readable path `doctor` reports rather than gates, and this
    # task is not the place to change that contract. The report is the assertion.
    assert main(["doctor", "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["components"]["service"] == {
        "status": "degraded",
        "reason": "service_inactive",
    }
    assert report["healthy"] is False
    assert tuple(supervisor.liveness_command()) in invoked


@pytest.mark.parametrize(("supervisor", "kind"), _SUPERVISORS)
def test_doctor_names_the_supervisor_it_asked(
    tmp_path, monkeypatch, capsys, supervisor, kind
) -> None:
    """An operator reading the report can tell which supervisor answered.

    Without it a false negative reads identically on both platforms: "service inactive" from a
    `systemctl` that is not installed says nothing about the service, and everything about the
    probe.
    """
    _arrange(tmp_path, monkeypatch, supervisor, liveness_exit_zero=True)

    assert main(["doctor", "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["service_supervisor"] == kind.value


def test_doctor_runs_the_ported_liveness_argv_and_nothing_systemd_specific_on_a_mac(
    tmp_path, monkeypatch, capsys
) -> None:
    """The regression this task closes: no `systemctl` is run when the host is a launchd host."""
    launchd = LaunchdSupervisor(
        interpreter=Path("/opt/ra/bin/python3"),
        home=Path("/Users/tester"),
        uid=501,
        homebrew_prefix=lambda: None,
    )
    invoked = _arrange(tmp_path, monkeypatch, launchd, liveness_exit_zero=True)

    assert main(["doctor", "--json"]) == 0

    capsys.readouterr()
    assert not any("systemctl" in argv[0] for argv in invoked), invoked
    assert ("pgrep", "-U", "501", "-f", "remote-agents serve") in invoked


@pytest.mark.parametrize(
    ("supervisor", "meaning"),
    [
        pytest.param(_SUPERVISORS[0].values[0], LivenessMeaning.RUNNING, id="systemd"),
        pytest.param(_SUPERVISORS[1].values[0], LivenessMeaning.RUNNING, id="launchd"),
    ],
)
def test_doctor_says_what_a_green_service_component_actually_establishes(
    tmp_path, monkeypatch, capsys, supervisor, meaning
) -> None:
    """A green `service` is not the same sentence on both supervisors, so the report says which.

    Both adapters currently answer "running" -- systemd via `is-active --quiet`, launchd via
    `pgrep`, which replaced `launchctl print` precisely because `print` exits zero for any
    *bootstrapped* job, including one that exited and is deliberately not being restarted. The
    field is reported rather than assumed because that agreement is a fact about today's two
    adapters, not a guarantee of the port: a supervisor able to confirm only registration would
    say so here instead of being read as "healthy".
    """
    _arrange(tmp_path, monkeypatch, supervisor, liveness_exit_zero=True)

    assert main(["doctor", "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["components"]["service"]["status"] == "healthy"
    assert report["service_liveness"] == meaning.value


def test_both_supervisors_now_answer_the_same_liveness_question() -> None:
    """Parity is the goal, and `LivenessMeaning` is what makes it a claim rather than a hope.

    The two used to differ: `launchctl print` could only report "registered", so a green
    service on a Mac meant less than the same word on Linux. Switching the launchd probe to
    `pgrep` -- which answers by exit status and so keeps the no-parsing rule -- made both
    genuinely answer "running".

    `REGISTERED` stays in the vocabulary rather than being deleted now that nothing returns it.
    It is what a future adapter whose supervisor can only confirm registration would have to
    declare, and declaring it is exactly what stops such an adapter passing itself off as
    equivalent -- which is the failure this member was added to surface.
    """
    systemd, launchd = (parameters.values[0] for parameters in _SUPERVISORS)

    assert systemd.liveness_meaning is LivenessMeaning.RUNNING
    assert launchd.liveness_meaning is LivenessMeaning.RUNNING
    assert LivenessMeaning.REGISTERED in set(LivenessMeaning)


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("darwin", SupervisorKind.LAUNCHD), ("linux", SupervisorKind.SYSTEMD)],
)
def test_the_host_is_matched_to_its_own_supervisor(monkeypatch, platform, expected) -> None:
    """DEC-054 calls `_supervisor_for_host` the single place the platform is decided.

    Every other test in this file monkeypatches it in order to inject an adapter, so until this
    existed the function itself was covered by nothing: inverting its branch -- darwin to
    systemd -- left the whole suite green while `doctor` probed for a tool that is not
    installed. The one place a decision is made is the one place worth pinning.
    """
    from remote_agents import bootstrap

    monkeypatch.setattr(bootstrap.sys, "platform", platform)

    assert bootstrap._supervisor_for_host().kind is expected


def test_a_host_that_cannot_be_described_is_a_config_error_not_a_traceback(monkeypatch) -> None:
    """The adapters refuse a home they cannot render faithfully; `serve` must survive saying so.

    `_supervisor_for_host` is reached from `serve` and the local surface, neither of which
    installs anything, so an adapter's `ValueError` would surface there as a traceback rather
    than as the handled bad-configuration path every other such answer travels.
    """
    from remote_agents import bootstrap
    from remote_agents.config import ConfigError

    def _refuses(*_args, **_kwargs):
        # The real refusal, raised the way the real adapter raises it. Driven through a stub
        # because the adapters read the host in a `default_factory` captured at import, so a
        # colon cannot be injected into `Path.home` after the fact -- and the behaviour under
        # test is the *conversion*, not which input triggers it.
        raise ValueError("supervisor home must not contain a colon: /Users/a:b")

    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    monkeypatch.setattr(bootstrap, "LaunchdSupervisor", _refuses)

    with pytest.raises(ConfigError, match="cannot be described"):
        bootstrap._supervisor_for_host()
