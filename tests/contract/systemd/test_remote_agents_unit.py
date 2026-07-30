"""The installed user-service boundary is deliberate and remains secret-free."""

from pathlib import Path

UNIT = Path("systemd/remote-agents.service")


def test_user_unit_has_private_paths_bounded_restart_and_tmux_survival() -> None:
    contents = UNIT.read_text(encoding="utf-8")

    assert "WorkingDirectory=%h/dev/infra/remote-agents" in contents
    assert "EnvironmentFile=%h/.config/remote-agents/telegram.env" in contents
    assert "ExecStart=%h/dev/infra/remote-agents/.venv/bin/remote-agents serve" in contents
    assert "Restart=on-failure" in contents
    assert "RestartSec=5s" in contents
    assert "TimeoutStopSec=30s" in contents
    assert "KillMode=process" in contents
    assert "UMask=0077" in contents
    assert "ProtectHome=" not in contents
    assert "PrivateTmp=" not in contents


def test_user_unit_contains_no_secret_literal() -> None:
    contents = UNIT.read_text(encoding="utf-8")

    assert "=" not in next(
        line for line in contents.splitlines() if line.startswith("EnvironmentFile=")
    ).removeprefix("EnvironmentFile=")
    assert "123456:" not in contents


def test_transient_probe_keeps_the_same_kill_mode_without_production_configuration() -> None:
    contents = Path("systemd/remote-agents-test.service").read_text(encoding="utf-8")

    assert (
        "ExecStart=%h/dev/infra/remote-agents/.venv/bin/python -m remote_agents.service_probe"
        in contents
    )
    assert "KillMode=process" in contents
    assert "EnvironmentFile=" not in contents
