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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
