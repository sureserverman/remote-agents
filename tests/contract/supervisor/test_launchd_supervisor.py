"""The generated LaunchAgent plist carries its whole environment, because launchd gives it none.

launchd expands nothing -- a plist holds strings -- and it starts an agent with `_PATH_STDPATH`
rather than with a login shell's PATH. The shipped systemd unit could stay silent about both
because a user manager inherits an environment and `%h` expands; a plist that stayed silent
about either would name paths that do not exist and start a service that cannot find the
tooling it drives. These tests pin that the rendering answers both questions itself, and that
generating it did not quietly drop the operative intent the shipped unit carries.

The whole file runs on Linux. A plist is arithmetic on paths, `plistlib` is stdlib, and the
Homebrew probe is injectable, so nothing here asks the host to be a Mac.
"""

from __future__ import annotations

import os
import plistlib
import stat
import sys
from pathlib import Path
from typing import Any

from remote_agents.adapters.supervisor import registered_supervisors
from remote_agents.adapters.supervisor.launchd import (
    LABEL,
    PLIST_NAME,
    LaunchdSupervisor,
    homebrew_prefix,
)
from remote_agents.ports.service_supervisor import (
    ServiceSupervisor,
    SupervisorKind,
    artifact_paths_to_remove,
)

#: A rendering that depends on nothing about the host: fixed interpreter, home and uid, and a
#: probe that answers without looking at a disk. The claims about the real host are made
#: separately, by the two tests that need them.
ELSEWHERE = LaunchdSupervisor(
    interpreter=Path("/opt/ra/bin/python3"),
    home=Path("/Users/tester"),
    uid=501,
    homebrew_prefix=lambda: Path("/opt/homebrew"),
)


def _plist(supervisor: LaunchdSupervisor) -> dict[str, Any]:
    """The rendered artifact, parsed back the way launchd will parse it."""
    (artifact,) = supervisor.artifacts()
    return plistlib.loads(artifact.content.encode("utf-8"))


def _path_entries(supervisor: LaunchdSupervisor) -> list[str]:
    return _plist(supervisor)["EnvironmentVariables"]["PATH"].split(":")


def test_launchd_artifact_is_a_property_list_launchd_can_parse() -> None:
    """Serialised by `plistlib`, so the file is XML that round-trips rather than hand-built."""
    (artifact,) = ELSEWHERE.artifacts()

    assert artifact.path == Path(f"/Users/tester/Library/LaunchAgents/{PLIST_NAME}")
    assert artifact.content.startswith("<?xml")
    assert artifact.content.endswith("\n")

    parsed = plistlib.loads(artifact.content.encode("utf-8"))
    assert parsed["Label"] == LABEL


def test_launchd_plist_spells_every_path_absolutely() -> None:
    """There is no `%h` on this side, and nothing expands a `~` inside a plist string.

    Every path the plist names -- the artifact's own location, the program, the working
    directory, and each PATH entry the service will search -- has to be absolute at render
    time or it names nothing at all.
    """
    parsed = _plist(ELSEWHERE)
    (artifact,) = ELSEWHERE.artifacts()

    assert artifact.path.is_absolute()
    assert parsed["ProgramArguments"][0] == "/opt/ra/bin/remote-agents"
    assert parsed["WorkingDirectory"] == "/Users/tester"
    for argument in parsed["ProgramArguments"]:
        assert "%h" not in argument
        assert "~" not in argument
    for entry in _path_entries(ELSEWHERE):
        assert entry.startswith("/"), entry
        assert "~" not in entry


def test_launchd_plist_starts_the_console_script_with_the_same_arguments_as_the_unit() -> None:
    """`serve --config <config>`, from the console script beside the installing interpreter."""
    parsed = _plist(ELSEWHERE)

    assert parsed["ProgramArguments"] == [
        "/opt/ra/bin/remote-agents",
        "serve",
        "--config",
        "/Users/tester/.config/remote-agents/config.toml",
    ]


def test_launchd_plist_names_the_running_interpreters_install_prefix() -> None:
    """Rendered with no arguments, the plist starts the console script beside *this* python."""
    parsed = _plist(LaunchdSupervisor())

    assert parsed["ProgramArguments"][0] == str(Path(sys.executable).parent / "remote-agents")


def test_launchd_plist_keeps_the_job_alive_only_when_it_failed() -> None:
    """`KeepAlive = {SuccessfulExit: False}` is the plist spelling of `Restart=on-failure`.

    A bare `KeepAlive = true` would restart the service after a clean exit too, which is a
    different policy from the one the shipped unit has run under.
    """
    parsed = _plist(ELSEWHERE)

    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert parsed["RunAtLoad"] is True


def test_launchd_plist_keeps_the_shipped_units_operative_lifecycle() -> None:
    """`RestartSec`, `TimeoutStopSec` and `KillMode=process` each have a launchd analogue.

    `AbandonProcessGroup` is the load-bearing one. launchd kills anything left in the job's
    process group when the job dies; setting it stops that, which is what `KillMode=process`
    buys on the other side -- the managed tmux sessions outlive a stop of the service that
    launched them.
    """
    parsed = _plist(ELSEWHERE)

    assert parsed["AbandonProcessGroup"] is True
    assert parsed["ExitTimeOut"] == 30
    assert parsed["Umask"] == 0o077

    # `RestartSec=5s` is deliberately NOT mirrored. `ThrottleInterval` is the nearest key and
    # it is a *floor* on respawn frequency, already 10 seconds by default -- so writing 5 would
    # have halved launchd's own crash-loop protection while reading like it added some. Its
    # absence is the setting, which is exactly the kind of thing that gets "helpfully" restored
    # later, so it is asserted rather than left to the reader.
    assert "ThrottleInterval" not in parsed

    # Without these two a LaunchAgent's output goes to /dev/null and the macOS service is
    # undiagnosable -- there is no journald on this side to fall back to.
    assert parsed["StandardOutPath"].endswith("/.local/state/remote-agents/remote-agents.log")
    assert parsed["StandardErrorPath"].endswith("/.local/state/remote-agents/remote-agents.err")


def test_launchd_plist_carries_no_credential_in_its_environment() -> None:
    """A plist's contents are readable through `launchctl print`; the secret is not in one.

    Task 2.0 retired `EnvironmentFile=` so that exactly one parser reads the credential file,
    and this is the side where putting it back would also publish it: `EnvironmentVariables`
    is echoed by `launchctl print`, which any process on the machine may run. The service
    reads its own secret in-process.
    """
    environment = _plist(ELSEWHERE)["EnvironmentVariables"]

    assert set(environment) == {"PATH"}


def test_launchd_plist_path_is_derived_from_the_probed_homebrew_prefix() -> None:
    """The prefix is whatever brew reported, so Intel's `/usr/local` renders as correctly."""
    apple_silicon = _path_entries(ELSEWHERE)
    intel = _path_entries(
        LaunchdSupervisor(
            interpreter=Path("/opt/ra/bin/python3"),
            home=Path("/Users/tester"),
            uid=501,
            homebrew_prefix=lambda: Path("/usr/local"),
        )
    )

    assert "/opt/homebrew/bin" in apple_silicon
    assert "/opt/homebrew/sbin" in apple_silicon
    assert "/usr/local/bin" not in apple_silicon

    assert "/usr/local/bin" in intel
    assert "/usr/local/sbin" in intel
    assert "/opt/homebrew/bin" not in intel


def test_launchd_plist_path_falls_back_to_both_standard_locations_without_homebrew() -> None:
    """No brew on the host is not a reason to guess which prefix a later one would use."""
    entries = _path_entries(
        LaunchdSupervisor(
            interpreter=Path("/opt/ra/bin/python3"),
            home=Path("/Users/tester"),
            uid=501,
            homebrew_prefix=lambda: None,
        )
    )

    assert "/opt/homebrew/bin" in entries
    assert "/usr/local/bin" in entries


def test_launchd_plist_path_carries_the_uv_tool_bin_and_the_standard_directories() -> None:
    """`~/.local/bin` is where uv puts the console script, and it is not in `_PATH_STDPATH`.

    The four standard directories are what launchd hands an agent on its own. Everything
    before them is what a login shell would have added and launchd does not.
    """
    entries = _path_entries(ELSEWHERE)

    assert entries[0] == "/Users/tester/.local/bin"
    assert entries[-4:] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]


def test_launchd_homebrew_probe_runs_brew_at_an_absolute_path_not_through_path(
    tmp_path: Path, monkeypatch
) -> None:
    """The probe must work in the environment the drill host actually has.

    Non-interactive SSH to the Stage 3 drill host has `_PATH_STDPATH` plus cargo and *not*
    `/opt/homebrew/bin`, so a bare `brew --prefix` reports "command not found" there even
    though `/opt/homebrew/bin/brew` is sitting on the disk. Emptying PATH here reproduces that
    exactly: the probe still has to answer, because it never asked PATH in the first place.
    """
    absent = tmp_path / "opt" / "homebrew" / "bin" / "brew"
    present = tmp_path / "usr" / "local" / "bin" / "brew"
    present.parent.mkdir(parents=True)
    present.write_text(f"#!/bin/sh\necho {tmp_path}/usr/local\n", encoding="utf-8")
    present.chmod(present.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", "")

    assert homebrew_prefix((absent, present)) == tmp_path / "usr" / "local"


def test_launchd_homebrew_probe_answers_none_when_no_brew_is_installed(tmp_path: Path) -> None:
    """Absent is a real answer, and the one this Linux host gives; it is not an error."""
    assert homebrew_prefix((tmp_path / "opt" / "homebrew" / "bin" / "brew",)) is None


def test_launchd_verbs_are_argv_the_caller_runs() -> None:
    """`bootstrap`/`bootout`/`kickstart` per `launchctl(1)`; `load` and `unload` are legacy."""
    assert ELSEWHERE.install_command() == (
        "launchctl",
        "bootstrap",
        "gui/501",
        f"/Users/tester/Library/LaunchAgents/{PLIST_NAME}",
    )
    assert ELSEWHERE.remove_command() == ("launchctl", "bootout", f"gui/501/{LABEL}")
    assert ELSEWHERE.start_command() == ("launchctl", "kickstart", f"gui/501/{LABEL}")
    # Deliberately not `launchctl print`: it exits 0 for a bootstrapped-but-dead job, so it
    # answers "is it registered". `pgrep` answers "is it running" by exit status alone, which
    # is what lets this adapter honestly declare the same LivenessMeaning as the systemd one.
    assert ELSEWHERE.liveness_command() == (
        "pgrep",
        "-U",
        "501",
        "-f",
        "remote-agents serve",
    )


def test_launchd_domain_target_uses_the_uid_it_was_given() -> None:
    """The uid is a constructor input so a test can pin it; the host's is only the default."""
    pinned = LaunchdSupervisor(
        interpreter=Path("/opt/ra/bin/python3"),
        home=Path("/Users/tester"),
        uid=1000,
        homebrew_prefix=lambda: None,
    )

    assert pinned.remove_command() == ("launchctl", "bootout", f"gui/1000/{LABEL}")
    assert LaunchdSupervisor().install_command()[2] == f"gui/{os.getuid()}"


def test_launchd_supervisor_satisfies_the_service_supervisor_port() -> None:
    """The second implementor: the protocol has to be met by both or the registry is a lie."""
    assert isinstance(ELSEWHERE, ServiceSupervisor)
    assert ELSEWHERE.kind is SupervisorKind.LAUNCHD


def test_launchd_removal_sweeps_every_path_this_adapter_has_ever_owned() -> None:
    """`artifact_paths_to_remove` over this adapter is what an uninstall has to delete.

    The retired half is empty, and empty is the honest answer: no released version of this
    project has ever written a plist -- `git log --all -- '*.plist'` finds no such file -- so
    there is no stranded path for DEC-051's sweep to pick up. An invented entry here would be
    a path an uninstaller goes and deletes on the strength of a claim that is not true.
    """
    assert ELSEWHERE.retired_artifact_paths() == ()
    assert artifact_paths_to_remove(ELSEWHERE) == (
        Path(f"/Users/tester/Library/LaunchAgents/{PLIST_NAME}"),
    )


def test_launchd_adapter_is_reachable_through_the_registry() -> None:
    """Task 2.5 sweeps every registered adapter's artifacts; the set has to be enumerable."""
    kinds = {supervisor.kind for supervisor in registered_supervisors()}

    assert SupervisorKind.LAUNCHD in kinds
