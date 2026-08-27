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
by `launchctl print`, which **any process running as this user** may run against this domain --
printing another user's GUI domain needs root, so the exposure is same-user rather than
machine-wide. That is the reachable case and not a weaker one: a co-resident agent session runs
as the owner, which is the threat `ports/private_directory.py` already refuses a planted symlink
over. So a token in the plist is a token readable by anything the owner is running.

Task 2.0 retired `EnvironmentFile=` on the
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

from remote_agents.ports.service_supervisor import (
    LivenessMeaning,
    SupervisorArtifact,
    SupervisorKind,
)

LABEL = "remote-agents"
"""The launchd job label, and every domain target's last component.

Not reverse-DNS, which is the convention rather than a requirement, and deliberately so: this
project has no domain, so any `com.…` label would be a claim of ownership over a namespace
nobody here holds. Keeping the label equal to the systemd unit's stem also means `doctor`
reports one service name on both platforms instead of two spellings of the same thing.
"""

PLIST_NAME = f"{LABEL}.plist"

#: The plist filenames this version installs, and the ones it used to -- DEC-051's ledger, the
#: same shape the systemd adapter keeps and for the same reasons, which are argued there.
INSTALLED_PLIST_NAMES: tuple[str, ...] = (PLIST_NAME,)

#: What launchd is told to write the job's output to, named once because two places need it: the
#: plist that asks for them and the ledger that must take them away. Spelled out twice, they
#: would drift the moment either changed -- and the drift would be silent, because a stale name
#: in the ledger deletes nothing and a missing one leaves output behind.
_LOG_NAMES = ("remote-agents.log", "remote-agents.err")

RETIRED_PLIST_PATHS: tuple[str, ...] = ()
"""Nothing, for a simpler reason than on the systemd side: no released version of this project
has ever installed a plist at all. `git log --all --diff-filter=A --name-only -- '*.plist'` finds
none, and `git log --all -S LaunchAgents` finds only the port. There is no history here to
strand, and a name invented to look complete is a file an uninstaller would go and delete.
"""

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


#: Every character that means something to an extended regular expression.
#:
#: `pgrep -f` takes an ERE, not a literal, and a path is not a regex: measured on the drill
#: host, `pgrep -U <uid> -f 'a+b(c'` exits **2** for a malformed pattern. `_command_succeeds`
#: reads any non-zero exit as "not running", so an unescaped `+` or `(` anywhere in the install
#: path would have turned a bad-pattern error into a confident, permanent "the service is down".
_ERE_METACHARACTERS = ".[]\\()*+?{}|^$"


def _literal_pattern(value: Path) -> str:
    """Render a path so `pgrep -f` matches it literally rather than as a pattern."""
    return "".join(
        f"\\{character}" if character in _ERE_METACHARACTERS else character
        for character in str(value)
    )


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
                # Matching `_command_succeeds`' bound in `bootstrap`. brew can stall on an
                # auto-update check, and this runs while rendering an artifact for an
                # installer -- a probe with no bound turns "Homebrew is busy" into "the
                # installer never returns". A timeout answers `None`, which is a defined
                # result here rather than an error.
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        prefix = Path(completed.stdout.strip()) if completed.stdout.strip() else None
        if completed.returncode == 0 and prefix is not None and prefix.is_absolute():
            return prefix
    return None


@dataclass(frozen=True, slots=True, kw_only=True)
class LaunchdSupervisor:
    """`ServiceSupervisor` for a per-user launchd domain, rendering its own LaunchAgent.

    **`home` has no default**, for the reason written out on `SystemdSupervisor`: a defaulted
    home let a test construct the registered adapters and drive `remove_daemon` through them,
    which deleted the real artifacts on the developer's own machine. This adapter's sweep names
    `~/Library/LaunchAgents/remote-agents.plist` and both launchd log files, so the same test on
    a Mac would have taken all three.

    Every *other* host-specific value is a constructor parameter with a host default rather than
    a read of the process environment, so a test renders a plist that depends on nothing about
    the machine running it -- which is what lets this adapter be exercised in full on the Linux
    host the project is developed on. `uid` is here for that reason and not only for symmetry:
    it is the domain target `launchctl` is addressed with, and a test that could not pin it
    would be asserting against whoever ran it.

    `homebrew_prefix` is injected for the same reason one step further out. The default probes
    the disk and shells out; a test passing a lambda gets a deterministic PATH without the host
    needing Homebrew, or needing to not have it.
    """

    home: Path
    interpreter: Path = field(default_factory=lambda: Path(sys.executable))
    uid: int = field(default_factory=os.getuid)
    homebrew_prefix: Callable[[], Path | None] = homebrew_prefix

    kind: ClassVar[SupervisorKind] = SupervisorKind.LAUNCHD

    #: Answered by `pgrep` against the running process, not by `launchctl` -- so this really
    #: is "running", matching the systemd side rather than merely resembling it.
    liveness_meaning: ClassVar[LivenessMeaning] = LivenessMeaning.RUNNING

    def __post_init__(self) -> None:
        """Enforce the absolute-path invariant every docstring here asserts.

        `ProductionPaths.for_home` refuses a relative home with a `ConfigError`; this is the
        same rule, applied where the *artifacts* are rendered. Upheld by convention it was
        upheld by nothing: the wired path only ever passes `Path.home()`, so a relative home
        would arrive from a test fixture or a future caller and render a definition full of
        relative paths that the supervisor resolves against its own working directory --
        silently, and differently on each platform. `ValueError` rather than `ConfigError`
        because an adapter may not import `remote_agents.config` under ARCH-02.
        """
        if not self.home.is_absolute():
            raise ValueError(f"supervisor home must be absolute: {self.home}")
        if not self.interpreter.is_absolute():
            raise ValueError(f"supervisor interpreter must be absolute: {self.interpreter}")
        if ":" in str(self.home):
            # `search_path` joins on `:`, so a colon in the home directory would silently
            # split one entry into two -- putting a truncated prefix on PATH and losing
            # `~/.local/bin`, which is where this project's own console script lives. A colon
            # is legal in an APFS name, and PATH simply cannot represent it.
            raise ValueError(f"supervisor home must not contain a colon: {self.home}")

    @property
    def log_directory(self) -> Path:
        """Where the agent's stdout and stderr are kept, under the owner's private state.

        Beside the rest of this project's state rather than in `~/Library/Logs`, so one
        `state_directory` holds everything the service writes. launchd creates these files
        itself before the job runs, and does so **without** applying `Umask` -- that governs
        what the *job* creates -- so they land 0644 and the directory's own mode is what keeps
        them from being world-readable.
        """
        return self.home / ".local" / "state" / "remote-agents"

    @property
    def plist_path(self) -> Path:
        """`~/Library/LaunchAgents/`, spelled out because a plist cannot defer the home."""
        return self.home / "Library" / "LaunchAgents" / PLIST_NAME

    @property
    def service_target(self) -> str:
        """`gui/<uid>/<label>`, the target the three per-service verbs address.

        **The GUI domain requires a console login, and that is a deliberate, documented
        constraint of this deployment rather than an oversight.** `launchctl(1)` distinguishes
        the two per-user domains explicitly: a `user/<uid>` domain "may exist independently of
        a logged-in user", while a `gui/<uid>` "user-login domain is created when the user logs
        in at the GUI". So with `gui/`, the service loads when the owner logs in at the Mac's
        screen and not before -- after a reboot with nobody at the console there is no domain to
        bootstrap into, and the plist is never read.

        Chosen by the owner (2026-08-24) with `user/<uid>` and a root `LaunchDaemon` as the
        alternatives. The trade accepted: the macOS service is available only while the owner is
        logged in, in exchange for keeping the job in the same session as the owner's GUI
        applications -- which is where the agent CLIs it launches expect to live -- and keeping
        the whole service unprivileged and reading a 0600 file out of the owner's own home,
        which the LaunchDaemon option would have changed.

        Anything that documents macOS setup has to say this out loud; a Mac that has rebooted
        and is sitting at the login window is a Mac where this service is legitimately absent,
        and that must not read as a fault.
        """
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
            # `TimeoutStopSec=30s`: how long SIGTERM has before launchd escalates to SIGKILL.
            "ExitTimeOut": 30,
            # `KillMode=process`, and the load-bearing one. launchd otherwise kills whatever is
            # left in the job's process group when the job dies, which would take the managed
            # tmux sessions down with a restart of the service that merely launched them.
            "AbandonProcessGroup": True,
            # `RestartSec=5s` has **no** launchd analogue and deliberately none is written.
            # `ThrottleInterval` is the nearest key and it is a *floor* on respawn frequency,
            # already 10s by default -- so mirroring the digit 5 across would have *halved*
            # launchd's own crash-loop protection while reading like it added some. Omitted is
            # stricter than the obvious translation.
            #
            # The service logs to stderr and leans on the supervisor to keep it, which on Linux
            # is journald -- the runbook and three acceptance documents all say
            # `journalctl --user -u remote-agents.service`. A LaunchAgent has no such default:
            # without these two keys its output goes to /dev/null and the macOS service is
            # undiagnosable, which would leave Stage 3's drill with no procedure for a wrong
            # reading. Apple's own guidance is to set these rather than redirect to /dev/null.
            "StandardOutPath": str(self.log_directory / _LOG_NAMES[0]),
            "StandardErrorPath": str(self.log_directory / _LOG_NAMES[1]),
            "Umask": 0o077,
        }
        content = plistlib.dumps(definition, fmt=plistlib.FMT_XML).decode("utf-8")
        return (SupervisorArtifact(path=self.plist_path, content=content),)

    def installed_artifact_paths(self) -> tuple[Path, ...]:
        """The plist, **and the two log files launchd creates on this job's behalf.**

        Neither log file is rendered here -- launchd opens `StandardOutPath` and
        `StandardErrorPath` itself, before the job runs -- but both exist *because* this plist
        named them, inside a directory this installer created, and an uninstaller that left them
        would leave the state directory holding daemon output after the daemon was removed. That
        is the gate criterion "the state directory contains no daemon artifact afterwards",
        failing on the one platform the criterion was written for; it was found by driving the
        real adapter through install-then-remove rather than by reading, because the test
        asserting it ran against a fake whose install creates nothing.

        This is why the installed half of the ledger is "every path this version *causes to
        exist*" rather than "every path it writes". The systemd side names its `enable` symlink
        for the same reason.
        """
        return (
            *(self.plist_path.parent / name for name in INSTALLED_PLIST_NAMES),
            *(self.log_directory / name for name in _LOG_NAMES),
        )

    def definition_path(self) -> Path:
        """The plist, and not the two log files launchd opens beside it.

        The distinction this member exists for is at its sharpest here: `installed_artifact_paths`
        deliberately answers wider than the definition, so on this adapter "where is the daemon"
        and "what does removal sweep" are genuinely different sets rather than the same one.
        """
        return self.plist_path

    def retired_artifact_paths(self) -> tuple[Path, ...]:
        """The ledger's retired half; the reasoning is on `RETIRED_PLIST_PATHS`."""
        return tuple(self.home / relative for relative in RETIRED_PLIST_PATHS)

    def required_directories(self) -> tuple[Path, ...]:
        """`~/Library/LaunchAgents` and the log directory, both needed before the first load.

        The log directory is the one that bites. launchd opens `StandardOutPath` and
        `StandardErrorPath` before the job runs, so on a fresh Mac the service never reaches
        the code that would have created its own state directory -- the job fails to start for
        a reason that has nothing to do with the service.
        """
        return (self.plist_path.parent, self.log_directory)

    def reload_command(self) -> tuple[str, ...]:
        """Nothing, and empty is the answer rather than a gap.

        launchd reads a plist at `bootstrap` time and caches no fragment the way systemd does,
        so there is no reload verb to run and inventing one would mean naming a `launchctl`
        subcommand that does something else. The installer skips an empty tuple.
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
        """Ask whether the service is **running**, by exit status alone.

        Not `launchctl print`. That was the obvious probe and it answers the wrong question:
        measured against real jobs on a Mac, it exits 0 for a job whose `state = not running`
        exactly as for a running one, because zero means *bootstrapped*. The cases an operator
        actually runs `doctor` for -- the binary moved, a config error, a permanent spawn
        failure -- all leave the job bootstrapped and dead, so a report built on that probe was
        green precisely when it needed not to be. Narrowing `print` is not available either:
        `launchctl(1)` says of its output "Do NOT rely on the structure".

        `pgrep` sidesteps the whole problem. The man page's prohibition is on parsing `print`'s
        structure, not on asking something else, and `pgrep` answers by exit status: 0 when a
        matching process exists, 1 when none does. That is the same shape as
        `systemctl is-active --quiet`, which is why both adapters can now honestly declare
        `LivenessMeaning.RUNNING`.

        Scoped to this user's own processes (`-U`) and matched against the full command line
        (`-f`) so that the console script plus its `serve` subcommand *matches* the service
        rather than the interpreter name, which every Python process shares. Matches, not
        identifies: `-f` is a substring match, so any of this user's processes whose argv
        happens to contain that same string would satisfy it too. `-U` is what keeps the
        practical risk small, and the port's exit-code-only contract is what stops it being
        narrowed further.

        **Matched on the command name, not on the install prefix**, and that is a correction
        rather than a preference. An earlier version built the pattern from `self.interpreter`,
        so the probe searched for whatever path was current *in the process asking the
        question*. That is wrong in the ordinary case, not merely across upgrades: `doctor` is
        routinely run from a repository checkout's own virtualenv, or through `uv run`, while
        the service runs from wherever it was installed -- so the probe looked for a path the
        healthy process did not have and reported it dead. Anchoring on `remote-agents serve`
        makes the answer independent of who is asking.

        **The cost, accepted knowingly:** `-f` is an unanchored substring match, so a
        `remote-agents serve` the owner started by hand in a terminal satisfies this too, even
        with the launchd job dead. That is a defensible reading -- the service *is* running --
        but it is not the same claim as "the supervisor has it up", and no exit-code-only
        `launchctl` verb can make the narrower one. Recorded in DEC-054.

        `/usr/bin/pgrep` is inside `_PATH_STDPATH`, so unlike tmux or uv it resolves even from
        the bare environment launchd would hand a job.
        """
        return ("pgrep", "-U", str(self.uid), "-f", f"{_literal_pattern(Path(LABEL))} serve")
