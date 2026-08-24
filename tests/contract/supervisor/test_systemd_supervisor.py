"""The generated systemd unit says where it is, rather than asking the supervisor.

The shipped unit at `systemd/remote-agents.service` deferred three paths to `%h` and hardcoded
the checkout underneath it. Both halves stop working the moment the same service definition has
to be produced for launchd, which has no home specifier at all, so the adapter renders every
path absolute at the moment it writes the file. These tests pin that, and pin that generating
the unit did not quietly drop any of the operative directives the shipped one carries.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from remote_agents.adapters.supervisor import registered_supervisors
from remote_agents.adapters.supervisor.systemd import SystemdSupervisor
from remote_agents.ports.service_supervisor import (
    ServiceSupervisor,
    SupervisorKind,
    artifact_paths_to_remove,
)

#: A rendering that depends on nothing about the host, for the assertions about path *shape*.
#: The claims about the real interpreter are made separately, by the tests that need it.
ELSEWHERE = SystemdSupervisor(interpreter=Path("/opt/ra/bin/python3"), home=Path("/home/tester"))


def _unit_contents(supervisor: SystemdSupervisor) -> str:
    (artifact,) = supervisor.artifacts()
    return artifact.content


def _user_manager_environment() -> dict[str, str] | None:
    """The environment `systemd-analyze --user` needs, or `None` if this host cannot give it.

    `--user` builds a real user manager to parse against, and that manager refuses to
    initialise without `XDG_RUNTIME_DIR` -- "Failed to lookup RuntimeDirectory path" -- which
    is unset under a test runner started outside a login session even though `/run/user/<uid>`
    is right there. Filling it in is what keeps this check running on exactly the hosts that
    can make it, rather than skipping on the developer machine it was written for.
    """
    runtime_directory = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_directory:
        default = Path("/run/user") / str(os.getuid())
        if not default.is_dir():
            return None
        runtime_directory = str(default)
    return {**os.environ, "XDG_RUNTIME_DIR": runtime_directory}


def test_systemd_unit_names_the_running_interpreters_install_prefix() -> None:
    """Rendered with no arguments, the unit starts the console script beside *this* python."""
    contents = _unit_contents(SystemdSupervisor())

    prefix = Path(sys.executable).parent
    assert f"ExecStart={prefix}/remote-agents serve --config " in contents


def test_systemd_unit_defers_no_path_to_a_home_specifier() -> None:
    """No `%h`, and no checkout path that only exists because `%h` used to expand to one.

    `%h` is the specifier a plist cannot have, so a unit that still used it would be describing
    a service the other adapter cannot describe. The old expansion is asserted against too:
    dropping the specifier while keeping `~/dev/infra/remote-agents` spelled out would be the
    same coupling with the indirection removed.
    """
    contents = _unit_contents(ELSEWHERE)

    assert "%h" not in contents
    assert "dev/infra/remote-agents" not in contents
    for line in contents.splitlines():
        directive, _, value = line.partition("=")
        if directive in {"ExecStart", "WorkingDirectory"}:
            assert value.startswith("/"), line


def test_systemd_unit_renders_absolute_paths_from_the_two_constructor_inputs() -> None:
    """Interpreter and home are the whole of the input, so the output is fully determined."""
    contents = _unit_contents(ELSEWHERE)
    (artifact,) = ELSEWHERE.artifacts()

    assert artifact.path == Path("/home/tester/.config/systemd/user/remote-agents.service")
    assert (
        "ExecStart=/opt/ra/bin/remote-agents serve "
        "--config /home/tester/.config/remote-agents/config.toml" in contents
    )


def test_systemd_unit_keeps_the_shipped_units_operative_directives() -> None:
    """Generating the unit must not be how a hardening or lifecycle directive gets lost.

    `KillMode=process` is the load-bearing one: it is what lets the managed tmux sessions
    outlive a stop of the service that launched them.
    """
    contents = _unit_contents(ELSEWHERE)

    for directive in (
        "Type=simple",
        "Restart=on-failure",
        "RestartSec=5s",
        "TimeoutStopSec=30s",
        "KillMode=process",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "ProtectControlGroups=yes",
        "ProtectKernelTunables=yes",
        "After=network-online.target",
        "Wants=network-online.target",
        "WantedBy=default.target",
    ):
        assert directive in contents


def test_systemd_unit_carries_no_environment_mechanism() -> None:
    """Task 2.0 retired `EnvironmentFile=` so one parser reads the credential file.

    The generated unit is a second place that directive could come back, and it would come back
    silently: nothing greps a string this module builds at runtime the way the gate greps the
    shipped file. The service reads its own secret in-process.
    """
    contents = _unit_contents(ELSEWHERE)

    assert "EnvironmentFile=" not in contents
    assert not any(line.startswith("Environment=") for line in contents.splitlines())


def test_systemd_unit_passes_systemd_analyze_verify(tmp_path: Path) -> None:
    """The real interpreter's rendering is a unit systemd itself accepts.

    Rendered from `sys.executable` rather than from the fixed paths the other tests use,
    because `verify` resolves `ExecStart` on disk: a unit naming `/opt/ra` would fail here for
    a reason that has nothing to do with whether the rendering is correct.

    The skip is loud and `BLOCKED:`-prefixed, the way `tests/live` marks a check it could not
    make, because a green that silently means "systemd was not here" is the one result this
    test must never be confused with.
    """
    if shutil.which("systemd-analyze") is None:
        pytest.skip("BLOCKED: systemd-analyze is not installed")
    environment = _user_manager_environment()
    if environment is None:
        pytest.skip("BLOCKED: no XDG_RUNTIME_DIR for a --user manager to initialise against")

    (artifact,) = SystemdSupervisor().artifacts()
    # Verify keys off the filename's suffix, so the temporary copy has to keep the unit's name.
    written = tmp_path / artifact.path.name
    written.write_text(artifact.content, encoding="utf-8")

    completed = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(written)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr


def test_systemd_supervisor_satisfies_the_service_supervisor_port() -> None:
    """First implementor of the port, so somebody has to check the protocol is actually met."""
    assert isinstance(ELSEWHERE, ServiceSupervisor)
    assert ELSEWHERE.kind is SupervisorKind.SYSTEMD


def test_systemd_verbs_are_argv_the_caller_runs() -> None:
    """The four verbs, as the tuples `bootstrap`'s existing `systemctl` call sites already use."""
    unit = "remote-agents.service"

    assert ELSEWHERE.install_command() == ("systemctl", "--user", "enable", "--now", unit)
    assert ELSEWHERE.remove_command() == ("systemctl", "--user", "disable", "--now", unit)
    assert ELSEWHERE.start_command() == ("systemctl", "--user", "start", unit)
    assert ELSEWHERE.liveness_command() == (
        "systemctl",
        "--user",
        "is-active",
        "--quiet",
        unit,
    )


def test_systemd_removal_sweeps_every_path_this_adapter_has_ever_owned() -> None:
    """`artifact_paths_to_remove` over this adapter is what an uninstall has to delete."""
    assert artifact_paths_to_remove(ELSEWHERE) == (
        Path("/home/tester/.config/systemd/user/remote-agents.service"),
    )


def test_systemd_adapter_is_reachable_through_the_registry() -> None:
    """Task 2.5 sweeps every registered adapter's artifacts; the set has to be enumerable."""
    kinds = {supervisor.kind for supervisor in registered_supervisors()}

    assert SupervisorKind.SYSTEMD in kinds


@pytest.mark.parametrize(
    "awkward",
    ["my user", "o'brien", "50%off"],
    ids=["space", "apostrophe", "percent"],
)
def test_systemd_unit_survives_an_awkward_but_legal_home(tmp_path: Path, awkward: str) -> None:
    """systemd itself accepts the unit for every path shape the renderer claims to handle.

    This is the test whose absence let a real defect ship for a whole task. `verify` was only
    ever run against `SystemdSupervisor()` -- the clean host default -- so every branch of the
    quoting logic was unexercised against systemd, and two of them were wrong: a quoted
    `WorkingDirectory` is a fatal error (`path is not absolute`), and an unquoted apostrophe in
    `ExecStart` makes systemd discard the line entirely (`Unbalanced quoting, ignoring`, then
    `Service has no ExecStart=`). Both were rendered by the adapter and neither was caught.

    The paths are built for real, with a real executable at the end of them, because `verify`
    resolves `ExecStart` on disk -- a unit naming a path that does not exist fails for a reason
    that has nothing to do with quoting and would mask the thing under test.
    """
    if shutil.which("systemd-analyze") is None:
        pytest.skip("BLOCKED: systemd-analyze is not installed")
    environment = _user_manager_environment()
    if environment is None:
        pytest.skip("BLOCKED: no XDG_RUNTIME_DIR for a --user manager to initialise against")

    home = tmp_path / awkward
    home.mkdir()
    # The interpreter lives *outside* the awkward home on purpose. systemd constrains the
    # executable name specifically, so mixing the two would conflate "does the renderer handle
    # an awkward home" with "does systemd accept an awkward executable" -- and the second
    # question has its own test, which asserts the adapter refuses it up front.
    binaries = tmp_path / "venv" / "bin"
    binaries.mkdir(parents=True)
    for name in ("python3", "remote-agents"):
        executable = binaries / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)

    (artifact,) = SystemdSupervisor(interpreter=binaries / "python3", home=home).artifacts()
    written = tmp_path / artifact.path.name
    written.write_text(artifact.content, encoding="utf-8")

    completed = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(written)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{artifact.content}"
    # `verify` reports a bad directive on stderr while still exiting 0 for some classes, so the
    # diagnostics are asserted too rather than trusting the status alone.
    assert "not absolute" not in completed.stderr, completed.stderr
    assert "Unbalanced quoting" not in completed.stderr, completed.stderr


@pytest.mark.parametrize("hostile", ["new\nline", "carriage\rreturn"])
def test_systemd_refuses_a_path_that_would_inject_a_directive(hostile: str) -> None:
    """A newline in a path extends the unit with a line of someone else's choosing."""
    with pytest.raises(ValueError, match="control character"):
        SystemdSupervisor(interpreter=Path("/opt/ra/bin/python3"), home=Path(f"/home/{hostile}"))


@pytest.mark.parametrize("refused", ["o'brien", 'quote"d', "back\\slash"])
def test_systemd_refuses_an_interpreter_path_it_could_never_start(refused: str) -> None:
    """A path systemd will reject is refused here instead, while it can still be acted on.

    systemd's own rule, measured rather than assumed: quoting round-trips the path correctly
    and systemd *then* rejects it with "Executable name contains special characters", fatally.
    Rendering the unit anyway would move the failure to `systemctl start`, where the message
    names a character rather than a fix.

    The bound is asserted by the sibling test above, which still renders a home containing an
    apostrophe successfully: the restriction is on the executable name, so only the interpreter
    is constrained and an operator whose *home* is awkward is not turned away.
    """
    with pytest.raises(ValueError, match="quote or backslash"):
        SystemdSupervisor(interpreter=Path(f"/home/{refused}/bin/python3"), home=Path("/home/t"))
