"""What onboarding needs the host to already have, and what it may do about a gap.

Three things live here and they are deliberately one module: what is required, what state
each requirement is in, and what an operator would have to type to fix it. Splitting them
would put the remediation command somewhere that does not know whether it is needed.

**Nothing here decides anything from a version string.** DEC-002 says an installed executable
is available and its version is owner-managed diagnostic evidence, and this is the module most
tempted to break that rule -- a preflight is exactly where a version floor feels responsible.
It is not: this project's only claim on tmux is that it can host a server, which a version
number does not establish and a failed launch does, so a floor here would refuse hosts that
work and pass hosts that do not. The version is carried so a report can print it.

The two effects -- locating an executable, and asking it what version it is -- are parameters
rather than imports, which is what keeps this module in `application/` at all: it may not
shell out (ARCH-02 puts subprocess in adapters and composition roots), and the composition
root supplies both. It is the same shape `probe_profiles` uses for agent executables, and the
same reason: those two calls are the whole of what touches the host.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

#: The two states a requirement can be in. Closed, and closed at two on purpose: a third
#: ("too old", "unsupported") is the version gate DEC-002 forbids, arriving as an enum member
#: rather than as a comparison.
AVAILABLE = "available"
MISSING = "missing"

_STATES = (AVAILABLE, MISSING)

#: An executable that is installed and would not say what it is. Borrowed verbatim from
#: `probe_profiles`, which met the case first: a note, never a refusal.
VERSION_PROBE_FAILED = "version_probe_failed"

#: What this service cannot run without, as opposed to what it merely benefits from.
#:
#: `tmux` because every managed session is a tmux session and the control plane is nothing
#: without one. `git` because a project this tool launches an agent into is a checkout, and
#: the agent profiles all expect to be able to read its history.
#:
#: The agent CLIs themselves are deliberately absent: they are probed already, per profile,
#: by `probe_profiles`, and a host with only one of the five installed is a working host.
REQUIRED_DEPENDENCIES = ("tmux", "git")

#: How each requirement is asked. tmux answers `-V` and rejects `--version`, so the argument
#: is per-name rather than the single `--version` the curated agent profiles all share.
VERSION_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "tmux": ("-V",),
    "git": ("--version",),
}


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    """One requirement, whether the host has it, and -- separately -- why there is no version.

    `version` is evidence and `note` is diagnosis; neither is ever a reason to stop. The split
    is `ProfileAvailability`'s (DEC-045): one field carrying both a value and an explanation of
    its absence forces every reader to guess which it is holding.
    """

    name: str
    state: str
    version: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.state not in _STATES:
            raise ValueError(f"dependency state must be one of {_STATES}: {self.state}")
        if self.state == MISSING and self.version is not None:
            # Nothing answered, and here is what it answered. Refused rather than tidied
            # because a report that prints a version beside "missing" is one an operator
            # reasonably reads as "found, but wrong" -- which is the version gate again,
            # reconstructed by a reader out of a state that should not exist.
            raise ValueError("a missing dependency has no version")

    @property
    def satisfied(self) -> bool:
        """Whether onboarding may proceed on this requirement's account."""
        return self.state == AVAILABLE


def probe_dependencies(
    names: Sequence[str] = REQUIRED_DEPENDENCIES,
    *,
    resolve: Callable[[str], Path | None],
    run_version: Callable[[tuple[str, ...]], str],
) -> tuple[DependencyStatus, ...]:
    """Report each named requirement in the order it was asked for.

    Every name yields exactly one status, including the ones that are missing: a report an
    operator reads to find out what is wrong cannot answer by leaving the wrong thing out.
    """
    statuses: list[DependencyStatus] = []
    for name in names:
        located = resolve(name)
        if located is None:
            statuses.append(DependencyStatus(name=name, state=MISSING))
            continue
        argv = (str(located), *VERSION_ARGUMENTS.get(name, ("--version",)))
        try:
            version = _sanitized(run_version(argv))
        except OSError:
            statuses.append(DependencyStatus(name=name, state=AVAILABLE, note=VERSION_PROBE_FAILED))
            continue
        statuses.append(DependencyStatus(name=name, state=AVAILABLE, version=version))
    return tuple(statuses)


def _sanitized(value: str) -> str:
    """Reduce whatever an executable printed to one printable, bounded line.

    A version string is about to be rendered into an operator's terminal and, on the
    onboarding path, into a report. It is the output of a program this project did not write,
    so it is treated as untrusted text: first non-empty line only, non-printable characters
    dropped, length bounded. `probe_profiles._sanitize_version` does exactly this for agent
    executables and the reasoning is not specific to them.
    """
    line = next((part.strip() for part in value.splitlines() if part.strip()), "")
    if not line:
        raise OSError("version probe returned no text")
    return "".join(character for character in line if character.isprintable())[:160]


class PackageManager(Enum):
    """How this host installs system packages. The platform axis, and only that.

    **This is its own closed concept and it is not the supervisor kind wearing a hat.** The two
    happen to agree on the hosts this project ships for -- systemd beside apt, launchd beside
    Homebrew -- and reading one off the other would be the cheapest possible way to get the
    right answer for the wrong reason. DEC-054 makes `SupervisorKind` a label that this layer
    may not branch on, and the coincidence does not survive contact with reality anyway: a
    systemd host installs with `dnf` or `pacman` just as readily, and Homebrew runs on Linux.
    Package management and service supervision are two questions about a host, so they get two
    types, and `bootstrap.py` -- which is where DEC-015 puts the deciding -- answers both.

    An `Enum` rather than the string tokens `AVAILABLE`/`MISSING` use, because these are not the
    same kind of thing. Those are report vocabulary, serialised into `doctor --json` and read
    back by a human. This is a dispatch axis: a platform nobody has written a remediation for
    must fail where it is named, not silently miss every branch and render nothing.

    Closed at two because two is what this project has an installer for. A third member is a
    deliberate, reviewable edit that has to bring its own command with it.
    """

    APT = "apt"
    HOMEBREW = "homebrew"


#: Homebrew's own documented one-liner, from https://brew.sh, verbatim.
#:
#: Verbatim on purpose: it is upstream's published instruction, and an operator about to pipe a
#: remote script into `bash` should be able to compare what this tool printed against what
#: brew.sh says, character for character. Anything this project "improved" about the line would
#: be a difference they have to adjudicate at exactly the wrong moment.
HOMEBREW_INSTALL_INSTRUCTION = (
    '/bin/bash -c "$(curl -fsSL '
    'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
)

#: The privileged half of the apt remediation. Only `confirm`-gated code may ever run this argv.
_APT_INSTALL = ("sudo", "apt-get", "install", "-y")  # never run outside the confirm helper

_BREW_INSTALL = ("brew", "install")


@dataclass(frozen=True, slots=True)
class Remediation:
    """What to do about a gap: the line to show an operator, and -- separately -- what may run.

    The two are not the same answer and the type has to say which it is holding. A Mac with no
    Homebrew has a perfectly good instruction and *nothing this tool may execute*: `brew install
    tmux` on a host without `brew` is a command that cannot run, and offering it costs the
    operator a failed paste before they learn what they actually needed. So `command` is `None`
    there, and a caller asks `runnable` rather than sniffing `instruction` for a leading verb --
    which is the check that would quietly start passing the day the wording changed.

    `instruction` is `shlex.join(command)` whenever there is a command, enforced below rather
    than documented: a report that prints one field and an installer that runs the other are
    two renderings of one fact, and the failure mode of letting them drift is an operator who
    is shown a line that is not the line that ran.
    """

    instruction: str
    command: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not self.instruction.strip():
            raise ValueError("a remediation must say something an operator can act on")
        if self.command is not None:
            if not self.command:
                raise ValueError("a runnable remediation needs an argv")
            if self.instruction != shlex.join(self.command):
                raise ValueError("a remediation's instruction must be the command it runs")

    @property
    def runnable(self) -> bool:
        """Whether there is an argv a caller could offer to run, as opposed to text to read."""
        return self.command is not None


def render_remediation(
    missing: Sequence[str],
    *,
    package_manager: PackageManager,
    homebrew_installed: bool = True,
) -> Remediation:
    """Say how to install everything that is missing, in one command where one exists.

    One command for all of them rather than one per package: an operator fixing a fresh host
    should type once, and both package managers take a list. The order is the caller's, not
    sorted -- the caller's order is `REQUIRED_DEPENDENCIES`' order, which is the order the
    report they are looking at already printed, and re-sorting it here would make the fix line
    disagree with the list above it for no gain.

    `homebrew_installed` is a *parameter* because this module may not look: probing the host is
    the composition root's job, the same way `resolve` and `run_version` are handed to
    `probe_dependencies`. It is not generalised to "is the package manager installed" because
    the two sides are genuinely asymmetric -- `apt-get` is part of a Debian base system, while
    Homebrew is a third-party install a fresh Mac does not have -- and a generic flag would
    invite a caller to claim apt is absent, for which there is no bootstrap line to print.

    An empty `missing` is refused rather than rendered. `apt-get install -y` with no packages is
    not a no-op; it is a command that runs and does something else, and the caller that reached
    here with nothing missing has a bug this is the cheapest place to show them.
    """
    packages = tuple(missing)
    if not packages:
        raise ValueError("there is nothing to remediate")
    if package_manager is PackageManager.APT:
        return _runnable((*_APT_INSTALL, *packages))
    if package_manager is PackageManager.HOMEBREW:
        if not homebrew_installed:
            return Remediation(instruction=HOMEBREW_INSTALL_INSTRUCTION)
        return _runnable((*_BREW_INSTALL, *packages))
    raise ValueError(f"no remediation is defined for {package_manager}")


def _runnable(argv: tuple[str, ...]) -> Remediation:
    """Build the two renderings of one command from the command itself."""
    return Remediation(instruction=shlex.join(argv), command=argv)
