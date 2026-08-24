"""Install, remove, start and observe the daemon -- without naming a supervisor.

Two supervisors run this service: systemd on Linux, launchd on macOS. They agree on almost
no mechanical detail -- an INI unit under `~/.config/systemd/user/` with `%h` expansion
versus an XML plist under `~/Library/LaunchAgents/` with no home specifier at all,
`enable --now` versus `bootstrap`, an inherited user-manager environment versus a
`_PATH_STDPATH` that contains neither Homebrew's prefix nor `~/.local/bin`. None of those
differences is a fact the installer or `doctor` needs to hold (DEC-001), and this module is
what they hold instead: four verbs, and the two questions about ownership that DEC-051 makes
an installer answer.

**The verbs are argv, not methods that run.** A `ServiceSupervisor` hands back the command
and never executes it. Two things follow, and both were the point:

*Either adapter can be exercised on the other's platform.* Rendering a plist is arithmetic on
paths, so the launchd adapter is fully testable on the Linux host this project is developed
on, and the systemd adapter is testable on the Mac that Stage 3 drills. A port whose verbs
executed would have made each adapter's tests unrunnable on the machine most likely to be
running them.

*Liveness stays exit-code-only.* The caller runs the argv and reads the status; there is no
return channel here for parsed state, so there is nothing for a caller to parse. That is a
constraint, not an omission: `launchctl(1)` documents `print`'s output as not API -- "Do NOT
rely on the structure" -- so a port that carried structured status would be inviting exactly
the coupling that man page forbids, on the one side that forbids it. The systemd side already
answered liveness by exit code alone, so the shape is symmetric rather than a concession.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable


class SupervisorKind(Enum):
    """Which supervisor produced an answer, for a report that has to say so.

    A label, not a switch. Nothing in `application/` or `domain/` may branch on this -- that
    is what having a port is for -- but `doctor` reports what it checked, and "the service is
    not running" is a different sentence depending on which supervisor was asked. The set is
    closed and enumerated for the same reason the composition roots are (DEC-015): a third
    supervisor should be a deliberate, reviewable edit rather than a string that turns up.
    """

    SYSTEMD = "systemd"
    LAUNCHD = "launchd"


@dataclass(frozen=True, slots=True)
class SupervisorArtifact:
    """One file an adapter installs: where it goes, and the whole of what goes in it.

    Both supervisors' definitions reduce to this. Their *formats* do not agree and never
    will, which is why `content` is rendered text the adapter is wholly responsible for
    rather than a structure this module knows how to serialise -- a shared schema would have
    to be the union of an INI unit and an XML plist, and would leak both.

    `path` is absolute. launchd has no `%h`, so a plist cannot defer the home directory to
    the supervisor the way the shipped unit does; making every path absolute at render time
    is the only rule that holds on both sides.
    """

    path: Path
    content: str


@runtime_checkable
class ServiceSupervisor(Protocol):
    """The four verbs and the two ownership questions, in one vocabulary.

    Deliberately seven members. Each traces to something a caller needs: the goal names
    installing, removing, starting and observing; DEC-051 makes an installer name what it
    owns *and* what it used to own; and `doctor` has to say which supervisor answered.
    """

    kind: SupervisorKind

    def artifacts(self) -> tuple[SupervisorArtifact, ...]:
        """Every file this version installs, rendered and ready to write."""
        ...

    def retired_artifact_paths(self) -> tuple[Path, ...]:
        """Every path an *older* version installed, which this one must still remove.

        DEC-051's rule, applied to daemon definitions instead of hook groups: an artifact
        leaves `artifacts()` by *moving* here, never by disappearing. Dropping a path outright
        strands it -- removal sweeps what the installer knows it owns, so a definition no
        longer named is a definition no version of this tool can take away, and the operator
        cannot work around it by uninstalling first, because that would mean running the old
        uninstaller before taking the upgrade.
        """
        ...

    def install_command(self) -> tuple[str, ...]:
        """Register the written artifacts with the supervisor."""
        ...

    def remove_command(self) -> tuple[str, ...]:
        """Unregister the service. Deleting the files is `artifact_paths_to_remove`'s answer."""
        ...

    def start_command(self) -> tuple[str, ...]:
        """Start the registered service now.

        Separate from `install_command` because it is separate on both sides: systemd's
        `enable --now` and launchd's `bootstrap` register, and a running process afterwards
        is `start` and `kickstart` respectively. A caller that only wants the service up
        should not have to re-register it to get there.
        """
        ...

    def liveness_command(self) -> tuple[str, ...]:
        """A command whose **exit status alone** answers "is it running right now".

        Zero means running. Nothing about its output is part of this contract, and no caller
        may read it -- see this module's docstring on why that is load-bearing on the launchd
        side rather than merely tidy.
        """
        ...


def artifact_paths_to_remove(supervisor: ServiceSupervisor) -> tuple[Path, ...]:
    """Every path removal must sweep: what this version installs, and what any version did.

    The union, in one place, so neither adapter can implement DEC-051's sweep slightly
    differently -- which is the failure the decision was written after, where the installed
    set and the swept set were computed by the same predicate and a dropped entry silently
    left both.

    Order is installed-then-retired with duplicates dropped, so a path that appears in both
    (an adapter mid-migration naming the same file twice) is removed once.
    """
    seen: dict[Path, None] = {}
    installed = (artifact.path for artifact in supervisor.artifacts())
    for path in (*installed, *supervisor.retired_artifact_paths()):
        seen.setdefault(path, None)
    return tuple(seen)
