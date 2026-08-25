"""Write what a supervisor defines, register it, and take it away again -- for either supervisor.

Sub-plan 1 gave the port four verbs and left three of them with no caller: the adapters could
say what to write and what to run, and nothing did either. This module is that caller, and it is
deliberately one module rather than two, because everything platform-specific about installing a
daemon is already inside the argv and the artifact the port hands over. What is left is the same
on both sides -- make the directories, write the files, register -- and writing it once is what
stops the systemd path and the launchd path drifting into two different installers.

It lives in `adapters/` because it does I/O: it creates directories, writes files, and runs the
argv the port returns. `application/` may do none of those, which is the same boundary that put
the probe's `resolve` and `run_version` in its caller's hands.

**Order is the whole of the correctness here, and one ordering bug is invisible on Linux.**
launchd opens a job's `StandardOutPath` and `StandardErrorPath` *itself*, before the job's
process runs, so a plist naming a log directory the service would have created on startup names
one that does not exist on a fresh Mac -- and the job fails to start for a reason that has
nothing to do with the service. The directories therefore come first, always, on both platforms,
where the systemd side needs the same thing for the duller reason that `install(1)` creates no
parents.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from remote_agents.ports.private_directory import open_private_directory
from remote_agents.ports.service_supervisor import ServiceSupervisor, artifact_paths_to_remove


class DaemonInstallError(Exception):
    """A daemon this installer will not write, and the reason why."""


@dataclass(frozen=True, slots=True)
class DaemonOutcome:
    """What one install or removal actually did, in the shape `install-agent-hooks` reports.

    Two fields for the same reason that installer has two: a caller has to be able to tell "I
    changed something" from "there was nothing to change" without parsing the sentence it is
    about to print.
    """

    changed: bool
    summary: str
    succeeded: bool = True


def install_daemon(
    supervisor: ServiceSupervisor, *, run: Callable[[tuple[str, ...]], int]
) -> DaemonOutcome:
    """Make the directories, write the definition, and register it -- in that order, idempotently.

    **An unchanged definition registers nothing, and that is not just tidiness.** `launchctl
    bootstrap` exits non-zero for a job that is already bootstrapped, so an installer that
    re-registered an identical plist would report a failure on the most ordinary thing an
    operator ever does, which is run the command twice. Comparing the rendered bytes against
    what is on disk is the same test `install_agent_hooks` makes before it writes, and it yields
    the same sentence.

    **A changed definition is unregistered before it is registered again**, because `bootstrap`
    will not replace a loaded job -- the documented reload is `bootout` first. The removal's exit
    status is ignored: a definition that was never registered is the ordinary case on a first
    install, and it is not an error. The implied stop is deliberate, and sub-plan 1's drill on
    real hardware is what makes it safe: the managed tmux sessions outlive it, because
    `KillMode=process` and `AbandonProcessGroup` are exactly the directives that keep a session
    running when the control plane that launched it goes down.
    """
    for directory in supervisor.required_directories():
        # One side effect of that helper worth naming: it sets the *leaf* to 0700 even when the
        # directory already existed. `~/Library/LaunchAgents` is shared -- Homebrew services and
        # other tools register their own agents there -- so onboarding narrows a directory it
        # does not own. macOS creates it 0700 already, so this changes nothing in practice, and
        # the alternative (skipping the mode on an existing directory) would leave the symlink
        # walk without the property it exists to establish.
        if open_private_directory(directory) is None:
            # `Path.mkdir(exist_ok=True)` decides an existing entry is acceptable by calling
            # `is_dir()`, which *resolves symlinks* -- so a link standing where a daemon
            # directory belongs reports success and every write afterwards lands wherever it
            # points. This project has closed that hole three times already
            # (`ports.private_directory`, used by the activity spool, the database and the tmux
            # runtime), and a review caught this module reintroducing it. It is worth more here
            # than at those three: launchd creates a job's log files itself, *without* applying
            # the plist's `Umask`, so they land 0644 -- and the directory's mode is the only
            # thing keeping them out of another reader's hands. A link planted by a co-resident
            # process running as the same owner would hand them over.
            raise DaemonInstallError(
                f"refusing to install through a link or an entry that is not a private "
                f"directory: {directory}"
            )
    artifacts = supervisor.artifacts()
    changed = [artifact for artifact in artifacts if _differs(artifact.path, artifact.content)]
    paths = ", ".join(str(artifact.path) for artifact in artifacts)
    # **`running`, not `registered`, and the difference is not pedantry.** `liveness_meaning` is
    # `RUNNING` on both shipped adapters and the port is explicit that liveness cannot answer
    # whether a job is *registered* -- `REGISTERED` has no implementor, because narrowing
    # `launchctl print` means parsing output its own man page forbids relying on. So this
    # variable is named for what it actually knows, after a review caught an earlier version
    # calling it `registered` and justifying itself entirely with cases (a Mac before console
    # login, a hand-run `bootout`) where the job is genuinely absent -- while the signal it used
    # also fires for a service the operator deliberately stopped.
    running = run(supervisor.liveness_command()) == 0
    if not changed and running:
        return DaemonOutcome(False, f"daemon already current at {paths}")
    if not changed:
        # The definition is current and the service is down, and this installer **cannot tell
        # "stopped" from "never registered"** -- so it tries the cheaper, more surgical verb
        # first. `start_command()` starts an already-registered service without re-registering
        # it; on a host where the job is absent it fails, and the re-registration below is the
        # answer. That ordering is what keeps a deliberate `systemctl --user stop` from being
        # answered with a full unregister/re-register cycle.
        #
        # **It is still answered with a start, and that is the accepted trade.** `--install-daemon`
        # is an imperative -- it is `enable --now` on the systemd side, whose whole meaning is
        # "register and start" -- so an operator who runs it is asking for the service to be up,
        # and bringing up one they had stopped is doing what they asked rather than overriding
        # them. An operator who wants it down after onboarding stops it afterwards, or uses
        # `--remove`. Named here, and in the README, because it is a side effect a reader would
        # otherwise have to derive from two adapters' argv.
        if run(supervisor.start_command()) == 0:
            return DaemonOutcome(True, f"started the already-current daemon at {paths}")
    # Unregistered *before* the new bytes land, so a supervisor that reads the file at bootout
    # time reads the definition it was registered with rather than its replacement.
    run(supervisor.remove_command())
    for artifact in changed:
        _write_privately(artifact.path, artifact.content)
    # Between the write and the register, which is the only place it does anything: systemd
    # would otherwise `enable --now` a fragment it had already cached, starting the definition
    # this run just replaced. Skipped when the adapter has nothing to reload.
    reload_argv = supervisor.reload_command()
    if reload_argv:
        run(reload_argv)
    code = run(supervisor.install_command())
    written = ", ".join(str(artifact.path) for artifact in (changed or artifacts))
    verb = "installed" if changed else "re-registered"
    if code != 0:
        # **The exit status of the register is not ignored, and the one above it is.** The
        # asymmetry is deliberate and was the other half of a review finding: an unregister that
        # fails is the ordinary first-install case, because there was nothing registered -- while
        # a register that fails is a host with a definition on disk and no service running, and
        # reporting `changed=True` there tells the operator it worked. `doctor` would have
        # disagreed a moment later, which is a worse way to find out than being told.
        # "wrote" only when something was written. On the unchanged-down-and-unstartable path
        # this call wrote nothing -- the definition was already on disk -- and saying otherwise
        # tells an operator to go and look at a file this run did not touch.
        wrote = f"wrote {written} but" if changed else f"the definition at {written} is current but"
        return DaemonOutcome(True, f"{wrote} {supervisor.kind.value} refused to register it", False)
    return DaemonOutcome(True, f"{verb} the {supervisor.kind.value} daemon at {written}")


def remove_daemon(
    supervisor: ServiceSupervisor, *, run: Callable[[tuple[str, ...]], int]
) -> DaemonOutcome:
    """Unregister the service and delete every path any version of this tool ever installed.

    The sweep is `artifact_paths_to_remove`'s union of the installed and retired sets, which is
    DEC-051's rule: an artifact leaves the installed set by *moving* to the retired one, so a
    definition this version no longer writes is still one this version can take away. Nothing
    outside that union is touched -- a file that merely shares the directory was written by
    somebody else, and this installer has no way to give it back.

    The unregister command's exit status is ignored for the reason it is ignored on install: a
    host that was never installed to is not a failure, it is the answer.
    """
    unregistered = run(supervisor.remove_command())
    # `is_file()` alone follows the link and answers False for a broken one, so a dangling
    # symlink at an artifact path -- left by a partial failure, or by someone else -- was neither
    # removed nor reported by a sweep that claims to take away everything this tool ever
    # installed. `unlink` removes the link itself and never what it points at, so widening the
    # test cannot make removal reach further than it should.
    removed = [
        path for path in artifact_paths_to_remove(supervisor) if path.is_file() or path.is_symlink()
    ]
    if not removed:
        # The unregister's status is *not* consulted here, and that is deliberate rather than an
        # oversight repeated: `systemctl --user disable` on a unit that was never enabled exits
        # non-zero, which is the ordinary answer on a host that was never installed to.
        return DaemonOutcome(False, f"no daemon installed for {supervisor.kind.value}")
    swept = ", ".join(str(path) for path in removed)
    try:
        for path in removed:
            path.unlink()
    except OSError as error:
        # Reported the way `install_daemon` reports a failed register, rather than raised. This
        # loop had no handler at all, so a permission error -- or a concurrent deletion racing
        # the check just above it -- came out as a traceback from the one command an operator
        # runs when a host is already in a state they do not understand.
        return DaemonOutcome(True, f"could not remove every daemon file: {error}", False)
    if unregistered != 0:
        # **It is consulted here, where files were actually removed**, and the asymmetry is the
        # whole point. `install_daemon` already refuses to call a failed register a success,
        # because a definition on disk with nothing running must not read as one -- and the
        # mirror case read as success until a gate evaluator drove it: on a host with no session
        # bus, `disable` fails, the unit file is deleted anyway, and the `default.target.wants`
        # symlink `enable` wrote is left dangling with the operator told the daemon was removed.
        # The symlink is in the ledger now, so the sweep takes it; this is what stops the *other*
        # half -- a service the supervisor still believes in -- being reported as gone.
        return DaemonOutcome(
            True,
            f"removed {swept}, but {supervisor.kind.value} would not unregister the service",
            False,
        )
    return DaemonOutcome(True, f"removed the {supervisor.kind.value} daemon from {swept}")


def _differs(path: Path, content: str) -> bool:
    """Whether what is on disk is not already exactly what would be written."""
    try:
        return path.read_text(encoding="utf-8") != content
    except (OSError, UnicodeDecodeError):
        # Unreadable or not text is not "the same", so it is rewritten. A definition this tool
        # owns and cannot read is one it cannot reason about, and leaving it in place because the
        # comparison failed would be the one outcome nobody wants.
        return True


def _write_privately(path: Path, content: str) -> None:
    """Write a daemon definition owner-only, replacing any earlier one atomically.

    A temporary file beside the target and then a rename, so a supervisor reading the directory
    never sees a half-written definition, and `0o600` at creation so there is no window in which
    it is readable by anyone else. Neither artifact carries a credential -- the security suite
    sweeps every adapter to prove it -- but a daemon definition still names paths inside the
    owner's private tree, and the mode costs nothing.
    """
    # `mkstemp`, not a fixed `<name>.partial`. The fixed name was predictable, opened without
    # `O_EXCL` or `O_NOFOLLOW`, and reused across runs, which cost three properties at once: a
    # symlink planted there was **written through**, and `os.replace` then renamed the *link*, so
    # the installed unit became a symlink to a file outside the private directory that the next
    # run's byte comparison would read straight through -- a permanent redirection of `ExecStart`
    # that `doctor` reports as healthy. An ordinary file pre-planted 0666 kept its mode, because
    # `O_CREAT`'s mode is ignored for a file that already exists, which falsified this
    # function's own guarantee. And two concurrent installs collided on the one name.
    # `mkstemp` is `O_EXCL|O_NOFOLLOW`-equivalent, 0600 by construction, and unique per call.
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".partial")
    temporary = Path(name)
    written = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
        written = True
    finally:
        # `finally`, not `except OSError`: a Ctrl-C between the write and the rename is the most
        # likely non-OSError here, and it left the `.partial` behind. It is self-healing on the
        # next run, which is exactly why nobody would notice a file sitting in the supervisor's
        # own directory.
        if not written:
            temporary.unlink(missing_ok=True)
