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


#: The directives whose values are host paths, and so are *expected* to differ between the
#: shipped unit and a generated one -- that difference is the entire point of generating it.
_PATH_DIRECTIVES = frozenset({"WorkingDirectory", "ExecStart"})

_SHIPPED_UNIT = Path(__file__).resolve().parents[3] / "systemd" / "remote-agents.service"


def _operative_directives(unit_text: str) -> set[str]:
    """Every `key=value` line that is not a path and not prose."""
    return {
        line.strip()
        for line in unit_text.splitlines()
        if "=" in line
        and not line.startswith("[")
        and line.split("=", 1)[0] not in _PATH_DIRECTIVES
        and not line.startswith("Description=")
    }


def test_systemd_unit_keeps_the_shipped_units_operative_directives() -> None:
    """Generating the unit must not be how a hardening or lifecycle directive gets lost.

    Compared against the **shipped file itself**, not a list copied out of it. A hardcoded list
    pins the generated unit to whatever was true when the list was typed, so the shipped unit
    and the generated one could each be edited without the other noticing -- and while the
    operator's documented install path is still the shipped file, the two really are two
    definitions of one service. This makes them one.

    Path directives are excluded because differing there is the whole purpose of generating the
    unit; everything else must survive the translation, `KillMode=process` most of all, since it
    is what lets managed tmux sessions outlive a stop of the service that launched them.
    """
    shipped = _operative_directives(_SHIPPED_UNIT.read_text(encoding="utf-8"))
    generated = _operative_directives(_unit_contents(ELSEWHERE))

    assert shipped, "the shipped unit parsed to nothing -- this comparison would be vacuous"
    assert shipped <= generated, (
        "the generated unit dropped directives the shipped one carries: "
        f"{sorted(shipped - generated)}"
    )


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
    """`artifact_paths_to_remove` over this adapter is what an uninstall has to delete.

    The `default.target.wants` symlink is the second entry, and it is not one this project
    writes: `systemctl --user enable` creates it, `disable` removes it, and on a host where
    `disable` cannot run it stays -- dangling, in the supervisor's own directory, pointing at a
    unit file that has been deleted, and outside any sweep that only knew what this code wrote.
    """
    assert artifact_paths_to_remove(ELSEWHERE) == (
        Path("/home/tester/.config/systemd/user/remote-agents.service"),
        Path("/home/tester/.config/systemd/user/default.target.wants/remote-agents.service"),
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
    supervisor = SystemdSupervisor(
        interpreter=Path(f"/home/{refused}/bin/python3"), home=Path("/home/t")
    )

    # Constructing is fine and must stay fine: `_supervisor_for_host()` builds an adapter at
    # three sites that render nothing and only want `.kind`, and refusing there took `doctor`,
    # `serve` and the local surface down for an operator whose venv merely sits under a home
    # with an apostrophe in it. The refusal belongs to the artifact, so it fires on render.
    assert supervisor.kind is SupervisorKind.SYSTEMD

    with pytest.raises(ValueError, match="quote or backslash"):
        supervisor.artifacts()


def test_the_retired_unit_ledger_holds_home_relative_paths_not_bare_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retired entry can name a directory this version no longer installs to.

    The obligation is that an upgrade which *renames or relocates* an artifact must not strand
    the old one, and a bare filename joined to today's unit directory can only express the
    rename. Relative to home, so a retired entry can never name a file outside the operator's own
    tree -- a sweep must not be able to reach one.
    """
    from remote_agents.adapters.supervisor import systemd

    monkeypatch.setattr(
        systemd,
        "RETIRED_UNIT_PATHS",
        (".config/systemd/user/remote-agents-old.service", ".local/share/ra/legacy.service"),
    )
    supervisor = systemd.SystemdSupervisor(
        interpreter=Path("/opt/ra/bin/python3"), home=Path("/home/tester")
    )

    assert supervisor.retired_artifact_paths() == (
        Path("/home/tester/.config/systemd/user/remote-agents-old.service"),
        Path("/home/tester/.local/share/ra/legacy.service"),
    )


def test_every_retired_unit_entry_is_swept_and_is_not_also_installed(tmp_path: Path) -> None:
    """The per-entry pin, which activates the day an entry appears rather than needing an edit.

    The assertion this replaces was `RETIRED_UNIT_PATHS == ()`, and it was backwards: deleting a
    future retired entry -- the exact stranding DEC-051 exists to prevent -- made it pass again,
    while *adding* a legitimate entry made it fail. It penalised the right action and rewarded
    the wrong one. This iterates the ledger instead, so it is vacuous only while the ledger is
    empty and becomes a real sweep the moment it is not, with no test edit on the day that
    matters -- which is the day somebody is already editing this file and might delete rather
    than move.
    """
    from remote_agents.adapters.supervisor.installer import remove_daemon
    from remote_agents.adapters.supervisor.systemd import RETIRED_UNIT_PATHS, SystemdSupervisor

    supervisor = SystemdSupervisor(interpreter=tmp_path / "venv" / "bin" / "python3", home=tmp_path)
    installed = set(supervisor.installed_artifact_paths())
    for path in supervisor.retired_artifact_paths():
        assert path not in installed, f"{path} is in both halves of the ledger"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("an artifact an older version installed", encoding="utf-8")

    remove_daemon(supervisor, run=lambda argv: 0)

    for relative in RETIRED_UNIT_PATHS:
        assert not (tmp_path / relative).exists(), f"{relative} was left stranded"
