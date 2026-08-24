"""Render the LaunchAgent plist at install time, spelling out everything launchd will not.

The shipped `systemd/remote-agents.service` is the behavioural reference here as it is for the
systemd adapter, but the translation is not directive-for-directive. launchd differs from a
user manager in two ways this module has to answer for rather than mirror.

*It expands nothing.* A plist holds strings. There is no `%h`, no `~`, no shell -- so every
path is absolute at render time, derived from `Path(sys.executable)` and the home directory the
adapter was constructed with. That is the same rule the systemd adapter follows, and it is
`SupervisorArtifact.path`'s documented contract; on this side it is not a style choice.

*It inherits no environment.* A systemd user manager hands the service the manager's own
environment, which on the Linux host already contains everything the operator's login shell
put there. launchd hands an agent `_PATH_STDPATH` -- `/usr/bin:/bin:/usr/sbin:/sbin` -- and
nothing else. Neither Homebrew's prefix nor `~/.local/bin` is in it, and both are where this
service's tooling lives: uv installs the console script into `~/.local/bin`, and tmux and the
agent CLIs come from Homebrew. So the plist carries a PATH it computed, and `homebrew_prefix`
is how it computes the Homebrew half.

**The plist is not a place to put the credential.** `EnvironmentVariables` is echoed verbatim
by `launchctl print`, which any process on the machine may run against this domain, so a token
in the plist is a token published to the machine. Task 2.0 retired `EnvironmentFile=` on the
other side so that exactly one parser reads the credential file; here the same conclusion also
has a disclosure argument behind it, and `tests/integration/test_secret_sources.py` already
treats "no injected environment" as *the* launchd case. Only PATH goes in.

**What has no analogue, said out loud rather than dropped quietly.** The shipped unit's
hardening block -- `NoNewPrivileges`, `RestrictSUIDSGID`, `LockPersonality`,
`ProtectControlGroups`, `ProtectKernelTunables` -- is systemd's namespace and seccomp
machinery. launchd has no equivalent key for any of the five; macOS spends that budget through
a different mechanism entirely (code signing, entitlements, TCC), which is not something a
LaunchAgent plist configures. They are absent because there is nothing to write, not because
the translation forgot them. `After=`/`Wants=network-online.target` has no ordering analogue
either: `KeepAlive`'s `NetworkState` is a *run condition*, not an ordering edge, and adding it
beside `SuccessfulExit` would change the restart policy, because launchd ORs the KeepAlive
keys. The service must therefore tolerate starting before the network is up, and the
`SuccessfulExit` policy is what carries it if it does not.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from remote_agents.ports.service_supervisor import SupervisorArtifact, SupervisorKind

LABEL = "remote-agents"
"""The launchd job label, and every domain target's last component.

Not reverse-DNS, which is the convention rather than a requirement, and deliberately so: this
project has no domain, so any `com.…` label would be a claim of ownership over a namespace
nobody here holds. Keeping the label equal to the systemd unit's stem also means `doctor`
reports one service name on both platforms instead of two spellings of the same thing.
"""

PLIST_NAME = f"{LABEL}.plist"

#: Where a brew binary lives on each architecture's default install, as absolute paths.
#:
#: Probed in this order rather than found on PATH, because on the Mac Stage 3 drills PATH does
#: not contain either of them: a non-interactive SSH session there gets `_PATH_STDPATH` plus
#: cargo, so a bare `brew --prefix` answers "command not found" while `/opt/homebrew/bin/brew`
#: is sitting on the disk. That is the very environment-inheritance hazard this module exists
#: to work around, so resolving brew *through* an inherited PATH would be relying on the thing
#: being worked around. The prefix is still asked of brew rather than assumed from which
#: candidate matched, so a relocated or non-default prefix renders correctly too.
_BREW_BINARIES = (Path("/opt/homebrew/bin/brew"), Path("/usr/local/bin/brew"))

#: `_PATH_STDPATH`: what launchd puts in an agent's PATH when the plist says nothing.
_STANDARD_DIRECTORIES = (Path("/usr/bin"), Path("/bin"), Path("/usr/sbin"), Path("/sbin"))


def homebrew_prefix(candidates: tuple[Path, ...] = _BREW_BINARIES) -> Path | None:
    """Ask the installed brew where its prefix is, or answer `None` if there is no brew.

    `None` is a real answer, not a failure: it is what every Linux host says, including the one
    this adapter's tests run on, and the caller has a defined behaviour for it. Anything brew
    itself refuses to answer -- a non-zero exit, an empty line, a relative path -- is treated
    the same way, because a half-derived prefix in a PATH is worse than a documented fallback.
    """
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            completed = subprocess.run(
                [str(candidate), "--prefix"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            continue
        prefix = Path(completed.stdout.strip()) if completed.stdout.strip() else None
        if completed.returncode == 0 and prefix is not None and prefix.is_absolute():
            return prefix
    return None


@dataclass(frozen=True, slots=True)
class LaunchdSupervisor:
    """`ServiceSupervisor` for a per-user launchd domain, rendering its own LaunchAgent.

    Every host-specific value is a constructor parameter with a host default rather than a read
    of the process environment, so a test renders a plist that depends on nothing about the
    machine running it -- which is what lets this adapter be exercised in full on the Linux
    host the project is developed on. `uid` is here for that reason and not only for symmetry:
    it is the domain target `launchctl` is addressed with, and a test that could not pin it
    would be asserting against whoever ran it.

    `homebrew_prefix` is injected for the same reason one step further out. The default probes
    the disk and shells out; a test passing a lambda gets a deterministic PATH without the host
    needing Homebrew, or needing to not have it.
    """

    interpreter: Path = field(default_factory=lambda: Path(sys.executable))
    home: Path = field(default_factory=Path.home)
    uid: int = field(default_factory=os.getuid)
    homebrew_prefix: Callable[[], Path | None] = homebrew_prefix

    kind: ClassVar[SupervisorKind] = SupervisorKind.LAUNCHD

    @property
    def plist_path(self) -> Path:
        """`~/Library/LaunchAgents/`, spelled out because a plist cannot defer the home."""
        return self.home / "Library" / "LaunchAgents" / PLIST_NAME

    @property
    def service_target(self) -> str:
        """`gui/<uid>/<label>`, the target the three per-service verbs address."""
        return f"gui/{self.uid}/{LABEL}"

    def search_path(self) -> tuple[Path, ...]:
        """The PATH the service will run with: uv's bin, Homebrew's, then the standard four.

        Ordered the way a login shell would have ordered it. `~/.local/bin` leads because it is
        where uv puts this project's own console script, so a subprocess that re-enters this
        tool reaches the same install that is running. Homebrew follows, matching what
        `brew shellenv` prepends -- `bin` and `sbin` both, since Homebrew uses both. The
        standard four come last and are always present, so a missing Homebrew degrades to a
        shorter PATH rather than to no PATH.

        With no brew on the host, *both* default prefixes are named. Guessing one would be
        picking an architecture, and the wrong guess is a PATH entry that never resolves; both
        is honest about not knowing, and a nonexistent directory in a PATH costs a failed stat.
        """
        prefix = self.homebrew_prefix()
        prefixes = (
            (prefix,)
            if prefix is not None
            else tuple(binary.parent.parent for binary in _BREW_BINARIES)
        )
        homebrew = tuple(
            directory for base in prefixes for directory in (base / "bin", base / "sbin")
        )
        return (self.home / ".local" / "bin", *homebrew, *_STANDARD_DIRECTORIES)

    def artifacts(self) -> tuple[SupervisorArtifact, ...]:
        """The one file this version installs: the LaunchAgent, serialised by `plistlib`.

        Serialised rather than templated. The systemd side renders text because a unit file is
        text; a plist is a structure with a defined XML encoding, and hand-writing that
        encoding would mean hand-writing the escaping too. `plistlib` also enforces that what
        is written can be read back, which is the property the contract test asserts.

        `Umask` is `0o077`, and the octal literal is the point: launchd reads this key as a
        decimal integer, so the value that means "owner only" is 63. Writing the digits `77`
        because that is how a umask is spoken would silently install `0o115`.
        """
        definition = {
            "Label": LABEL,
            "ProgramArguments": [
                str(self.interpreter.parent / "remote-agents"),
                "serve",
                "--config",
                str(self.home / ".config" / "remote-agents" / "config.toml"),
            ],
            "WorkingDirectory": str(self.home),
            "EnvironmentVariables": {
                "PATH": ":".join(str(directory) for directory in self.search_path())
            },
            "RunAtLoad": True,
            # `Restart=on-failure`. A bare `KeepAlive = true` is a different policy: it would
            # also restart the service after it exited cleanly, which is what a stop looks like.
            "KeepAlive": {"SuccessfulExit": False},
            # `RestartSec=5s`. ThrottleInterval is a floor on respawn frequency rather than a
            # delay, but it is the only key that answers "do not spin on a crash loop".
            "ThrottleInterval": 5,
            # `TimeoutStopSec=30s`: how long SIGTERM has before launchd escalates to SIGKILL.
            "ExitTimeOut": 30,
            # `KillMode=process`, and the load-bearing one. launchd otherwise kills whatever is
            # left in the job's process group when the job dies, which would take the managed
            # tmux sessions down with a restart of the service that merely launched them.
            "AbandonProcessGroup": True,
            "Umask": 0o077,
        }
        content = plistlib.dumps(definition, fmt=plistlib.FMT_XML).decode("utf-8")
        return (SupervisorArtifact(path=self.plist_path, content=content),)

    def retired_artifact_paths(self) -> tuple[Path, ...]:
        """Nothing, and empty is the honest answer rather than an unfinished one.

        DEC-051's rule is that an artifact leaves `artifacts()` by *moving* here rather than by
        disappearing, so a path no current version installs is still a path every current
        version can take away. It has had no occasion to fire on this side, for a simpler
        reason than on the systemd side: no released version of this project has ever installed
        a plist at all. `git log --all --diff-filter=A --name-only -- '*.plist'` finds none,
        and `git log --all -S LaunchAgents` finds only the port added one commit ago. There is
        no history here to strand.

        Inventing an entry to look complete would be worse than empty. `artifact_paths_to_remove`
        feeds an uninstaller, so a path named here is a path something will go and delete on the
        strength of a claim that this project once installed it.
        """
        return ()

    def install_command(self) -> tuple[str, ...]:
        """`bootstrap` the plist into the per-user GUI domain.

        Not `launchctl load`, which `launchctl(1)` documents as legacy: it reports success
        whether or not the job was actually loaded, so an installer built on it cannot tell an
        install that worked from one that did not.
        """
        return ("launchctl", "bootstrap", f"gui/{self.uid}", str(self.plist_path))

    def remove_command(self) -> tuple[str, ...]:
        """`bootout` the service. Deleting the file is `artifact_paths_to_remove`'s answer."""
        return ("launchctl", "bootout", self.service_target)

    def start_command(self) -> tuple[str, ...]:
        """Start an already-bootstrapped service, without re-bootstrapping it to get there."""
        return ("launchctl", "kickstart", self.service_target)

    def liveness_command(self) -> tuple[str, ...]:
        """`print` against the service target: **exit status only**, and read the caveat.

        `launchctl(1)` says of this command's output "Do NOT rely on the structure", so the
        port forbids a caller parsing it and the exit status is the whole signal. There is no
        `launchctl` verb that answers liveness more narrowly without reading that output.

        **This asks a slightly different question than the systemd side does.** `systemctl
        --user is-active --quiet` is false for a unit that is loaded but not running.
        `launchctl print` exits zero for any *bootstrapped* service, including one that has
        exited cleanly and, under `KeepAlive = {SuccessfulExit: False}`, is deliberately not
        being restarted. So a zero here means "registered", and on the systemd side it means
        "running"; the two agree on the case that matters most -- a service that was never
        installed, or that was booted out, is non-zero on both -- and diverge on a service that
        is installed and stopped. The shared vocabulary does not make that difference go away,
        and narrowing it would mean parsing output the man page forbids parsing, so it is
        recorded here rather than papered over.
        """
        return ("launchctl", "print", self.service_target)
