"""The installed user-service boundary is deliberate and remains secret-free."""

import subprocess
import sys
from pathlib import Path

UNIT = Path("systemd/remote-agents.service")


def test_user_unit_has_private_paths_bounded_restart_and_tmux_survival() -> None:
    contents = UNIT.read_text(encoding="utf-8")

    assert "WorkingDirectory=%h/dev/infra/remote-agents" in contents
    # Retired deliberately: while this line existed, the credential file had two parsers --
    # systemd's here and this project's on macOS -- and they disagree about quoting, `;`
    # comments, lines without `=`, backslash escapes and continuations. The same bytes could
    # therefore yield two different bot tokens. The service reads the file itself now, so the
    # unit must not reintroduce a second reader.
    assert "EnvironmentFile=" not in contents
    assert "ExecStart=%h/dev/infra/remote-agents/.venv/bin/remote-agents serve" in contents
    assert "Restart=on-failure" in contents
    assert "RestartSec=5s" in contents
    assert "TimeoutStopSec=30s" in contents
    assert "KillMode=process" in contents
    assert "UMask=0077" in contents
    assert "NoNewPrivileges=yes" in contents
    assert "RestrictSUIDSGID=yes" in contents
    assert "ProtectHome=" not in contents
    assert "PrivateTmp=" not in contents


def test_user_unit_contains_no_secret_literal() -> None:
    contents = UNIT.read_text(encoding="utf-8")

    # The unit carries no environment mechanism at all now, which is a stronger guarantee
    # than the one this test used to make: it checked that the `EnvironmentFile=` line named a
    # path rather than inlining a value. With neither directive present, there is nowhere in
    # the unit for a credential to be written in the first place.
    assert "EnvironmentFile=" not in contents
    assert not any(line.startswith("Environment=") for line in contents.splitlines())
    assert "123456:" not in contents


def test_transient_probe_keeps_the_same_kill_mode_without_production_configuration() -> None:
    contents = Path("systemd/remote-agents-test.service").read_text(encoding="utf-8")

    assert (
        "ExecStart=%h/dev/infra/remote-agents/.venv/bin/python -m remote_agents.service_probe"
        in contents
    )
    assert "KillMode=process" in contents
    assert "EnvironmentFile=" not in contents


def test_the_executable_the_unit_starts_actually_runs() -> None:
    """The unit names a console script, so something has to check that script still imports.

    `ExecStart` above is asserted as *text*, which stays green while the entry point it names
    is broken -- and it was: moving `__main__`'s imports inside `if __name__ == "__main__"` to
    keep the agent-event hook from loading the composition root removed the module-level
    `main` that `[project.scripts]` resolves. Every in-process test drives `bootstrap.main`
    directly and the one subprocess test used `runpy`, which takes the `__main__` branch, so
    nothing noticed that the service could no longer start at all.

    Run through the real script rather than by importing it, because the failure was in the
    generated shim's `from remote_agents.__main__ import main`.
    """
    script = Path(sys.executable).parent / "remote-agents"
    assert script.exists(), f"no console script at {script}"

    completed = subprocess.run([str(script), "--help"], capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr
    assert "agent-event" in completed.stdout
    assert "Traceback" not in completed.stderr
