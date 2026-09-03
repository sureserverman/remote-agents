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

import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from remote_agents.ports.terminal_text import probe_version_line

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

#: The shape a name must have before anything else looks at it. Kept as a first, cheap refusal
#: with a readable message; it is **not** the boundary, for the reason below.
_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")


def _require_package_names(names: Sequence[str]) -> tuple[str, ...]:
    """Refuse anything this tool does not itself require, before it can become an argv word.

    **An allow-list, not a pattern, and the pattern that came first is why.** The first attempt
    validated against Debian's own policy for what a package may be *named* — `^[a-z0-9][a-z0-9
    +.-]*$` — which is the wrong grammar, because `apt-get install` accepts a superset of it.
    Measured on apt 2.8.3 with `-s`:

    - `apt-get -s install tmux-` reports **"The following packages will be REMOVED: tmux"**. A
      trailing `-` is apt's *remove* modifier, it is inside that character class, and
      a confirmed `sudo apt-get install -y tmux-` is one glyph away from the line the
      operator meant to approve. `+` is the matching install modifier.
    - `apt-get -s install x.deb` reports **180 packages to install**: when a literal name misses,
      apt falls back to matching it as an ERE, and `.` and `+` are metacharacters. One
      pattern-passing name is therefore an arbitrary package-set selector.

    So the syntactic check cannot be the boundary — apt has a second grammar layered on top of
    argv, and any pattern that permits the punctuation real package names contain also permits
    apt's own operators. What *is* a boundary is the set this tool installs, which is closed and
    is the same constant the probe reports on: a name that is not something this project
    requires has no business in a privileged argv, whoever asked for it.

    The probe is held to the same list for a smaller but live reason: a name containing `/`
    resolves through `shutil.which` as a literal path, so an unvalidated name would be
    *executed* with a version flag — unprivileged, but before anything is confirmed.

    A bare `str` is refused explicitly. `Sequence[str]` accepts one, and iterating it yields
    characters, so `render_remediation("tmux", …)` silently became four one-letter packages.
    """
    if isinstance(names, str):
        raise ValueError(f"package names must be a sequence, not one string: {names!r}")
    for name in names:
        if not _PACKAGE_NAME.match(name):
            raise ValueError(f"not a package name: {name!r}")
        if name not in REQUIRED_DEPENDENCIES:
            raise ValueError(f"not a dependency this tool installs: {name!r}")
    return tuple(names)


#: How each requirement is asked. tmux answers `-V` and rejects `--version`, so the argument
#: is per-name rather than the single `--version` the curated agent profiles all share.
VERSION_ARGUMENTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "tmux": ("-V",),
        "git": ("--version",),
    }
)


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

    **`run_version` may fail however it likes and this stays a report.** The contract is
    deliberately not "raises `OSError`", which is what an earlier version of this docstring
    implied by pointing at `probe_profiles` as the template to copy: that probe's real runner
    uses `subprocess.run(..., check=True, timeout=5)`, whose `CalledProcessError` and
    `TimeoutExpired` are **not** `OSError` subclasses, so an adapter written from the cited
    template crashed the whole dependency report on one executable that exited non-zero. A
    `tmux` present but unable to load a shared library is exactly the host onboarding exists to
    diagnose, and it is the host this raised a traceback on.

    So the failure is caught broadly, and this module refuses to specify an exception type it
    cannot name (it may not import `subprocess` under ARCH-02, which is the structural reason
    the narrow catch was unfixable in place). `bootstrap._console_features_available` reached
    the same conclusion for the same kind of probe, in the same words: a diagnostic probe
    reports, it never raises.
    """
    statuses: list[DependencyStatus] = []
    for name in _require_package_names(names):
        located = resolve(name)
        if located is None:
            statuses.append(DependencyStatus(name=name, state=MISSING))
            continue
        argv = (str(located), *VERSION_ARGUMENTS.get(name, ("--version",)))
        try:
            printed = run_version(argv)
        except Exception:  # noqa: BLE001 -- a diagnostic probe reports, it never raises
            printed = None
        version = None if printed is None else probe_version_line(printed)
        if version is None:
            statuses.append(DependencyStatus(name=name, state=AVAILABLE, note=VERSION_PROBE_FAILED))
            continue
        statuses.append(DependencyStatus(name=name, state=AVAILABLE, version=version))
    return tuple(statuses)


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

#: The complete set of argv heads a `Remediation` may carry, checked at construction.
#:
#: This is what turns the comment above from a claim about how callers behave into a property
#: of the type. It was the former, and an evaluator was right that "an unconfirmed install is
#: unrepresentable" overstated it. The honest claim now: a `Remediation` can only ever name a
#: package install, and there is exactly one function that offers to run one, which always
#: confirms. The residual, stated because hiding it would be worse: `instruction` is public and
#: splits back into an argv, so a caller determined to run a privileged command without asking
#: does not need this type's help -- what it can no longer do is get one from here and believe
#: it was vetted.
_INSTALLERS = (_APT_INSTALL, _BREW_INSTALL)


def _installer_prefix(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Return whichever known installer head this argv opens with, or the empty tuple."""
    for installer in _INSTALLERS:
        if argv[: len(installer)] == installer:
            return installer
    return ()


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

    **Three things are checked at construction, and each closes a way the type could have lied
    about what it holds.** The equality above is the first. The second is that the instruction
    is a single printable line: `shlex.quote` *wraps* a control character in quotes rather than
    removing it, so `tmux\r\x1b[2Kgit` satisfies the equality and still hands `announce` a
    string that erases the line the operator just read and redraws it -- and the instruction is
    the one thing here that a security decision is displayed on. The third is that the argv is
    a package install and nothing else: without it this type is "an arbitrary argv plus a
    matching label" while reading as "a vetted remediation", so any future caller that built one
    from something other than `render_remediation` would get arbitrary privileged execution with
    a `y` as the only remaining gate.
    """

    instruction: str
    command: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not self.instruction.strip():
            raise ValueError("a remediation must say something an operator can act on")
        if self.instruction.splitlines()[1:] or not self.instruction.isprintable():
            raise ValueError("a remediation's instruction must be one printable line")
        if self.command is not None:
            if not self.command:
                raise ValueError("a runnable remediation needs an argv")
            if self.instruction != shlex.join(self.command):
                raise ValueError("a remediation's instruction must be the command it runs")
            installer = _installer_prefix(self.command)
            if not installer:
                raise ValueError("a runnable remediation may only install packages")
            packages = self.command[len(installer) :]
            if not packages:
                # A confirmed `sudo apt-get install -y` alone passes the prefix check, exits 0,
                # would have had `InstallAttempt.resolved` report True for an install that
                # installed nothing -- the one lie `resolved`'s docstring exists to avoid. The
                # renderer already refuses an empty set; the *type* is what was repositioned as
                # the boundary, so the check belongs here too.
                raise ValueError("a runnable remediation must name something to install")
            _require_package_names(packages)

    @property
    def runnable(self) -> bool:
        """Whether there is an argv a caller could offer to run, as opposed to text to read."""
        return self.command is not None


def render_remediation(
    missing: Sequence[str],
    *,
    package_manager: PackageManager,
    homebrew_installed: bool,
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

    **It has no default, and losing the default was a fix.** `= True` meant that omitting the
    keyword on a fresh Mac rendered a *runnable* `brew install`, which the confirmation helper
    would then offer to execute on a host with no `brew` -- the exact case the unrunnable branch
    below exists to prevent, defeated by a forgotten argument. A safe answer that depends on the
    caller remembering to ask for it is the same anti-pattern `confirm_and_install` refuses one
    function down.

    An empty `missing` is refused rather than rendered: a caller that reached here with nothing
    missing has a bug, and this is the cheapest place to show them. *(The reason first written
    here -- that `apt-get install -y` with no packages "runs and does something else" -- was
    measured and is false on apt 2.8.3: `apt-get install -y -s` reports "0 upgraded, 0 newly
    installed" and no upgrade phase. The guard is right; the story about apt was not, and this
    project cites measurements rather than asserting them.)*
    """
    packages = _require_package_names(tuple(missing))
    if not packages:
        raise ValueError("there is nothing to remediate")
    if package_manager is PackageManager.APT:
        return _runnable((*_APT_INSTALL, *packages))
    if package_manager is PackageManager.HOMEBREW:
        # `is not True`, not `not`, and for the reason `confirm_and_install` gives one function
        # down: this is a bool-annotated parameter in a codebase with no type checker, and the
        # composition root reads its answer from a host probe that could as easily be an
        # environment variable, where `"false"` and `"0"` are both truthy. Getting it wrong here
        # shows an operator a `brew install` on a host with no `brew` and asks them to approve
        # it -- exactly what the unrunnable branch exists to prevent.
        if homebrew_installed is not True:
            return Remediation(instruction=HOMEBREW_INSTALL_INSTRUCTION)
        return _runnable((*_BREW_INSTALL, *packages))
    raise ValueError(f"no remediation is defined for {package_manager}")


def _runnable(argv: tuple[str, ...]) -> Remediation:
    """Build the two renderings of one command from the command itself."""
    return Remediation(instruction=shlex.join(argv), command=argv)


#: What one attempt at closing a gap actually did. Closed, and every member that is not
#: `INSTALLED` leaves the gap open -- there is no member meaning "probably fine".
INSTALLED = "installed"
DECLINED = "declined"
UNCONFIRMED = "unconfirmed"
INSTALL_FAILED = "install_failed"
MANUAL = "manual"

_OUTCOMES = (INSTALLED, DECLINED, UNCONFIRMED, INSTALL_FAILED, MANUAL)


@dataclass(frozen=True, slots=True)
class InstallAttempt:
    """What happened when onboarding offered to close one gap, and whether it closed.

    `resolved` is a single derived property rather than a field, so the four ways of *not*
    installing cannot drift apart from each other. A caller decides its exit status from this;
    it is deliberately not an exit code, because this layer does not own process semantics.
    """

    outcome: str
    instruction: str

    def __post_init__(self) -> None:
        if self.outcome not in _OUTCOMES:
            raise ValueError(f"install outcome must be one of {_OUTCOMES}: {self.outcome}")

    @property
    def resolved(self) -> bool:
        """Whether the installer ran and reported success.

        Deliberately not "the dependency is now present", which is what this said and is more
        than a zero exit status establishes: `brew install` exits 0 for a formula that was
        already there, and an installer can succeed at installing something other than what was
        asked for. Onboarding re-probes afterwards and reports what it then finds, so the
        stronger claim is made by the thing that can actually check it.
        """
        return self.outcome == INSTALLED


def confirm_and_install(
    remediation: Remediation,
    *,
    announce: Callable[[str], None],
    confirm: Callable[[str], bool] | None,
    run: Callable[[tuple[str, ...]], int],
    assume_yes: bool = False,
) -> InstallAttempt:
    """Show the command, get a real yes, and only then run it.

    **The confirmation is blocking on every path, and the shape of this function is the
    argument for that.** There is exactly one `run(...)` call site reachable, it sits after a
    single boolean that is either an explicit `--yes` or the answer to a prompt, and there is no
    default that resolves to true. The failure this guards against is not a caller forgetting
    to ask -- callers do not get the option -- it is a future flag whose default makes asking
    unnecessary, which is why `assume_yes` is a parameter with no host-derived default and why
    the non-interactive case below is a refusal rather than a fallback.

    **Consent is `is True`, not truthiness, and that is the fix a security pass earned.** This
    project runs no type checker, so `Callable[[str], bool]` is documentation and nothing more.
    The most obvious adapter anyone would write -- `confirm=lambda prompt: input(prompt)` --
    returns the string the operator typed, and `"n"`, `"no"`, `"N"` and `"abort"` are every one
    of them truthy: a plain refusal would have installed. The only refusals a truthiness test
    honoured were a bare Enter and a literal `False`. So this confirm step authorises a `sudo`
    install on exactly one value, the one whose identity it checks -- the same discipline the
    module already applies to `_STATES` and `_OUTCOMES`. `assume_yes` gets the same treatment
    for the same reason: it can arrive from an environment variable, and `"false"` and `"0"`
    are truthy too.

    `confirm=None` means *there is no terminal to ask*, which is the case an unattended
    installer arrives in, and it is answered `UNCONFIRMED`: nothing runs, the instruction is
    carried out to the caller to print, and the gap is reported open so the caller can exit
    non-zero. Treating "nobody was there to say no" as consent is the exact escalation this
    stage exists to prevent.

    **`announce` fires first on every path**, before the prompt and therefore before anything
    could run. An operator asked to approve an install has to be looking at the command they
    are approving; announcing after the fact describes what already happened.

    A remediation with nothing runnable -- a Mac with no Homebrew -- is `MANUAL`: it is shown,
    nothing is asked, and nothing is run. Asking a yes/no question this tool cannot act on
    either way would train the operator that the prompt is decorative.
    """
    announce(remediation.instruction)
    argv = remediation.command
    if argv is None:
        return InstallAttempt(outcome=MANUAL, instruction=remediation.instruction)
    if assume_yes is True:
        approved = True
    elif confirm is None:
        return InstallAttempt(outcome=UNCONFIRMED, instruction=remediation.instruction)
    else:
        approved = confirm(f"Run this now? [{remediation.instruction}]") is True
    if not approved:
        return InstallAttempt(outcome=DECLINED, instruction=remediation.instruction)
    try:
        code = run(argv)
    except Exception:  # noqa: BLE001 -- see below; a failed install is not a traceback
        # The confirmed installer could not even be started -- no `sudo` on a minimal
        # container, no `brew` despite the caller saying there was one. That is a failed
        # install, not a traceback out of a command whose whole job is to report on a host
        # that is missing things.
        #
        # Broad, and narrowed to `OSError` first, which was the same mistake `probe_dependencies`
        # was just fixed for: the runner a composition root will most plausibly copy is
        # `profiles._run_version`, i.e. `subprocess.run(..., check=True, timeout=5)`, whose
        # `CalledProcessError` and `TimeoutExpired` are not `OSError` subclasses. An apt that
        # exits non-zero, or one that hangs on a debconf prompt past its timeout, would have
        # come out as a traceback from a confirmed install. `run`'s contract is therefore
        # "return an exit status"; anything else it does is a failed install.
        return InstallAttempt(outcome=INSTALL_FAILED, instruction=remediation.instruction)
    outcome = INSTALLED if code == 0 else INSTALL_FAILED
    return InstallAttempt(outcome=outcome, instruction=remediation.instruction)
