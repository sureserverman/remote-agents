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
from remote_agents.ports.service_supervisor import SupervisorKind


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
    assert ("launchctl", "print", "gui/501/remote-agents") in invoked
