"""Compose onboarding, the dependency preflight, and the upgrade command."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from remote_agents import __version__
from remote_agents.adapters.supervisor.installer import (
    DaemonInstallError,
    install_daemon,
    remove_daemon,
)
from remote_agents.adapters.supervisor.launchd import LaunchdSupervisor
from remote_agents.adapters.supervisor.systemd import SystemdSupervisor
from remote_agents.application.dependencies import (
    MISSING,
    PackageManager,
    confirm_and_install,
    probe_dependencies,
    render_remediation,
)
from remote_agents.application.releases import (
    is_release_tag,
    newest_release,
    release_status,
    upgrade_available,
)
from remote_agents.config import (
    TELEGRAM_SECRET_VARIABLES,
    ConfigError,
    TelegramSecrets,
    describe_schema_drift,
    load_config,
    load_secrets,
    render_config,
)
from remote_agents.ports.argv_text import refuse_a_credential_shaped_value
from remote_agents.ports.service_supervisor import ServiceSupervisor, SupervisorKind
from remote_agents.production import ProductionPaths

_LOG = logging.getLogger(__name__)


def _onboard(arguments) -> int:
    """Take a freshly installed package to a configured, registered service on this host.

    The whole of onboarding is composition -- the dependency policy is in `application`, the
    schema is in `config`, the private tree is in `production`, and the daemon is behind the
    supervisor port -- so this function is where DEC-015 says it belongs and is deliberately not
    a fifth place that knows how to do any of those things itself.

    **It never clobbers what it did not write.** A config the operator has edited and a
    credential they pasted are both left exactly as found, with a line saying so; a re-run is
    what someone does when they are unsure what state a host is in, and it must be safe. The
    daemon is the one thing this tool does own outright, and even that is only rewritten when the
    definition actually changed.

    `--remove` takes away the daemon and nothing else. The config and the credential are the
    operator's data, and an uninstaller that deleted a bot token would be unrecoverable in the
    one way that matters.
    """
    if arguments.rejected_token is not None:
        # The value is already in argv and in shell history by the time this runs -- nothing
        # here can take it back, so the message says so and does not repeat it.
        raise ConfigError(
            "--bot-token takes no value here; use --bot-token-file <path> instead. "
            "The value you passed is now in this host's process list and shell history: "
            "rotate that token."
        )
    home = Path.home()
    paths = ProductionPaths.for_home(home)
    supervisor = _supervisor_for_host()
    wants_unit_directory = supervisor.kind is SupervisorKind.SYSTEMD
    if arguments.print_daemon_path:
        # **Before every branch that changes the host, and it changes nothing.** The credential
        # refusal above still runs first, deliberately: a value already in argv cannot be
        # un-leaked by anything here, so that check is not one a query may skip past. (This
        # comment claimed "before every other branch", which the line above it made false.)
        # This is what an operator runs
        # when they do not yet know what state the host is in, and what the upgrade contract's
        # own check runs to read the definition back -- so it must not create a directory,
        # write a config, or ask for a credential on the way to answering.
        #
        # `definition_path()`, never `artifacts()[0].path` (DEC-055): the systemd adapter
        # refuses at render time to describe an executable whose path holds a quote, so an
        # answer reached through the renderer would be unavailable on precisely the host whose
        # operator most needs to find the file.
        #
        # One line, no prose around it. The Stage 2 gate substitutes this into
        # `grep -rn … "$(…)"`, where a second line becomes one argument holding a newline --
        # a filename nothing can open, so `grep` exits 2 and the check's leading `!` reports
        # success having read no file at all.
        print(supervisor.definition_path())
        return 0
    if arguments.remove:
        try:
            outcome = remove_daemon(supervisor, run=_run_command)
        except ValueError as error:
            # `SystemdSupervisor` refuses at *render* time to describe an executable whose path
            # holds a quote or a backslash, and that refusal is deliberate. It once fired here,
            # so a home containing an apostrophe got a traceback out of the uninstaller.
            #
            # It no longer fires on this path at all: `artifact_paths_to_remove` reads
            # `installed_artifact_paths()`, which does not render, so the host this tool declines
            # to install to is no longer the host it cannot uninstall from. That was the DEC-051
            # hole and it is closed structurally rather than by reporting. This handler stays
            # because a future adapter could refuse for a reason removal does have to ask about,
            # and a traceback is the wrong way to learn that.
            print(
                f"this host cannot be described to its service supervisor: {error}",
                file=sys.stderr,
            )
            return 1
        print(outcome.summary)
        print(f"left alone: {paths.config_path} and {paths.environment_path}")
        return 0 if outcome.succeeded else 1
    # **Resolved, because a relative one reopened the defect this stage's own gate found.**
    # `--dev-root relative/tree` was written into the config verbatim, and `load_config` refuses
    # a `dev_root` that is not absolute -- so onboarding again wrote a config its own loader
    # rejects and registered a daemon against it, through a different validation rule than the
    # one that was just closed. Resolving here fixes the flag; `render_config` refusing a
    # relative path fixes the class, and the config is now proved loadable before any daemon is
    # registered, which fixes it for whatever rule is broken next.
    if arguments.dev_root is not None:
        # A path option in the same command as a credential file. The argparse redaction covers
        # what an *error* prints; this value is accepted, echoed by ordinary success output, and
        # written into the generated config where it stays on disk.
        refuse_a_credential_shaped_value("--dev-root", str(arguments.dev_root))
    dev_root = (arguments.dev_root or home / "dev").expanduser().resolve()
    interactive = sys.stdin.isatty()
    print("checking system dependencies:")
    if _dependency_preflight(assume_yes=arguments.yes, interactive=interactive):
        return 1
    paths.ensure_directories(include_unit_directory=wants_unit_directory)
    print(_prepared_dev_root(dev_root))
    print(_written_or_kept(paths.config_path, lambda: detected_config(home, dev_root)))
    try:
        print(_credential_summary(paths, arguments, interactive))
    except ConfigError as error:
        # Named without its cause's value: everything raised out of `onboarding_secrets` and
        # `write_private_environment` names a variable rather than a value, and this is the one
        # place that would undo that by printing whatever it caught.
        print(error, file=sys.stderr)
        return 1
    unloadable = describe_schema_drift(paths.config_path)
    if not unloadable["readable"]:
        # **Before the daemon, not after it.** A config the loader rejects is a service that
        # crash-loops under `Restart=on-failure` the moment it is registered, so registering one
        # against a config already known to be bad turns a diagnosable state into a running
        # fault. The closing report would have said so a few lines later -- with the daemon
        # already installed and looping.
        print(f"the configuration at {paths.config_path} cannot be loaded", file=sys.stderr)
        print(f"  {unloadable['detail']}", file=sys.stderr)
        return 1
    if arguments.install_daemon:
        try:
            outcome = install_daemon(supervisor, run=_run_command)
        except (DaemonInstallError, ValueError) as error:
            print(error, file=sys.stderr)
            return 1
        print(outcome.summary)
        if not outcome.succeeded:
            return 1
        if supervisor.kind is SupervisorKind.LAUNCHD:
            # Not a caveat in a document somewhere: `gui/<uid>` exists only once someone has
            # logged in at the Mac's screen (owner decision, DEC-054), so a Mac that has rebooted
            # and is sitting at the login window is a Mac where this service is legitimately
            # absent. Unless onboarding says it here, that reads as a fault.
            print("note: on macOS this service runs only while you are logged in at the screen")
        _wait_for_the_service(supervisor)
    return _report_on_the_onboarded_host(paths, installed_daemon=bool(arguments.install_daemon))


def _wait_for_the_service(supervisor: ServiceSupervisor, sleep=time.sleep) -> None:
    """Give a just-started service a moment to be running before the report asks.

    Both supervisors return before the service is up. `enable --now` returns for a `Type=simple`
    unit as soon as the process is forked, and `launchctl bootstrap` with `RunAtLoad` is
    asynchronous outright -- while the very next thing onboarding does is run `doctor`, whose
    exit status this command adopts. Measured on Linux, the race is won comfortably (the
    database appears ~0.17s after exec, and `probe_profiles` spends longer than that running five
    `--version` subprocesses first), but it is won *incidentally*: a cold first start on a slower
    host, or a Mac where nothing is warm, narrows a margin nobody chose.

    Bounded and quiet: at most a couple of seconds, and no output. A service that is not up by
    then has something wrong with it, and saying what is `doctor`'s job one line later -- this
    exists to stop the report answering before the question is fair, not to make it wait for an
    answer it is not going to get.
    """
    for attempt in range(_SERVICE_START_ATTEMPTS):
        # `_run_command`, the same helper `install_daemon` was handed, rather than
        # `_command_succeeds`: they answer the same question, and using two means a caller (or a
        # test) that substitutes one still reaches the other.
        if _run_command(supervisor.liveness_command()) == 0:
            return
        if attempt + 1 < _SERVICE_START_ATTEMPTS:
            sleep(_SERVICE_START_INTERVAL_SECONDS)


#: The `doctor` components onboarding is answerable for. Onboarding verifies the dependencies,
#: writes the credential file, and (when asked) registers the daemon -- so these are the ones
#: whose failure means *onboarding did not work*, as opposed to *this host is not finished*.
_COMPONENTS_ONBOARDING_OWNS = ("tmux", "telegram")

#: Owned only when `--install-daemon` was passed. Plain `onboard` registers nothing, so
#: `service_inactive` is its correct outcome rather than a fault -- failing on it would mean the
#: command could never succeed at what it was actually asked to do.
_COMPONENT_OWNED_ONLY_WITH_A_DAEMON = "service"

#: Named for what they are: real, reported, and nobody's to fix but the operator's. `core` wants
#: a projects registry that appears when a project is registered; `store` wants a database the
#: service creates on first run; `profiles` wants an optional third-party agent CLI (DEC-056).
_COMPONENTS_THE_OPERATOR_FINISHES = ("core", "store", "profiles")


def _report_on_the_onboarded_host(paths: ProductionPaths, *, installed_daemon: bool) -> int:
    """End onboarding with `doctor`'s own report, and answer for onboarding's own work.

    **A host that onboarded and cannot serve is not a successful onboarding.** The exit status is
    what a bootstrap script reads, so returning 0 beside a broken install would leave an
    unattended install believing it had finished -- the one failure a one-line installer must
    not have, because nobody is watching the output.

    **But the exit status answers for onboarding, not for the whole host (BL-001, owner's
    decision 2026-08-25).** It used to adopt `doctor`'s entire `healthy` bit, which made one bit
    carry two different statements -- *"the installation failed"* and *"you have not finished
    setting this up yet"* -- and resolved it as failure. On a genuinely fresh host the projects
    registry does not exist until a project is registered, so a completely correct install exited
    1 and an unattended installer concluded it had failed. Three components were implicated when
    this was raised; two resolved themselves as the installer improved, and `core` was the one
    left, which is the one onboarding can least claim to own.

    The rejected alternative was having onboarding create an empty registry so the check passes.
    That fabricates a file representing the operator's own projects in order to satisfy a
    detector, which is a worse answer than the wrong exit code was.

    **Nothing is hidden to achieve this.** The full report still prints, `doctor` still says the
    host is not wholly healthy, and what remains outstanding is still named -- as outstanding
    rather than as a fault. What changed is only which components may fail *this command*.

    The config is re-read from disk rather than carried from the generation step above, so what
    is reported on is the file the service will actually load. Onboarding may have kept an
    existing config rather than writing one, and that file is the one that matters.
    """
    drift = describe_schema_drift(paths.config_path)
    if not drift["readable"]:
        print(
            json.dumps(
                {
                    "healthy": False,
                    "config": drift,
                    "checked": False,
                    "platform": _host_platform(),
                },
                sort_keys=True,
            )
        )
        return 1
    # Deferred: the doctor report is bootstrap's (the doctor CLI owns it), and bootstrap
    # imports this module at module scope.
    from remote_agents.bootstrap import _doctor_report

    report = _doctor_report(paths, load_config(paths.config_path), drift)
    print(json.dumps(report, sort_keys=True))
    if report.get("healthy"):
        return 0
    owned = set(_COMPONENTS_ONBOARDING_OWNS)
    if installed_daemon:
        owned.add(_COMPONENT_OWNED_ONLY_WITH_A_DAEMON)
    # The report is machine-readable and the exit status is one bit, so an operator who gets a
    # 1 has to read a JSON blob to find out which of seven components said no. Naming them costs
    # a line and is the difference between "onboarding failed" and "install codex, or don't".
    # `status`/`reason`, which is what `health_report` actually emits. The first version asked
    # for a `ready` key that exists nowhere in the product -- it was written against a test
    # double that invented one, so the line never rendered and no test noticed. That is the same
    # failure as this stage's Blocking defect (a fixture supplying what the product does not),
    # reproduced inside the commit that diagnosed it, which is worth saying out loud.
    degraded = {
        name: component.get("reason")
        for name, component in (report.get("components") or {}).items()
        if isinstance(component, dict) and component.get("status") != "healthy"
    }
    mine = sorted(f"{name} ({reason})" for name, reason in degraded.items() if name in owned)
    theirs = sorted(f"{name} ({reason})" for name, reason in degraded.items() if name not in owned)

    # A config the loader rejects, or a credential file whose names will not resolve, are both
    # onboarding's own output -- and both can turn `healthy` false without appearing among the
    # components at all, so neither is reachable through the loop above.
    for section, key in (("config", "readable"), ("credential_file", "names_resolved")):
        carried = report.get(section)
        if isinstance(carried, dict) and not carried.get(key, True):
            mine.append(f"{section} ({key} is false)")

    if mine:
        print(f"onboarding did not complete: {', '.join(sorted(mine))}", file=sys.stderr)
        return 1
    if theirs:
        # stdout, not stderr, and worded as work rather than as fault. This is the whole of what
        # the decision changed: the same facts, in the same report, no longer failing a command
        # that did everything asked of it.
        print(f"onboarding complete. Still to do, and not part of onboarding: {', '.join(theirs)}")
        print("  These are yours to finish; `remote-agents doctor` reports them at any time.")
    return 0


def _prepared_dev_root(dev_root: Path) -> str:
    """Make the projects tree the generated config is about to name, before naming it.

    **This is the defect a gate evaluator found, and it is the same one this whole stage was
    written to close, one directory over.** `load_config` refuses a `paths.dev_root` that is not
    an *existing* directory -- which is exactly why the shipped example cannot be copied onto
    another host -- and the generator replaced the example's hardcoded `/home/user/dev` with a
    detected `~/dev` that nothing created. On a fresh Mac, which is the platform this exists for,
    onboarding wrote a config its own loader rejects, registered a daemon that then crash-looped
    against it under `Restart=on-failure`, and exited 1 with a message naming no path.

    Every test missed it because every fixture manufactured `~/dev` first: the suite created the
    precondition the product did not.

    Created rather than refused, because `~/dev` on a machine that has never had one is a
    directory the operator is about to want, not a mistake to report. An operator who keeps
    projects elsewhere says so with `--dev-root`. It is deliberately **not** 0700 and not part of
    `ProductionPaths`: this is the operator's own working tree, outside the private boundary that
    type declares, and tightening a directory this tool does not own is not its business.
    """
    if dev_root.is_dir():
        return f"projects tree: {dev_root}"
    try:
        dev_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ConfigError(f"cannot create the projects tree {dev_root}: {error}") from error
    return f"created the projects tree {dev_root}"


def _written_or_kept(path: Path, render) -> str:
    """Create a generated file owner-only, or keep what is there -- and say which happened.

    Three properties, and this function had none of them until a review took it apart against
    its own siblings. `path.exists()` **follows links and answers False for a dangling one**, so
    a symlink planted at `config.toml` pointing outside the private tree was written through:
    measured, it created a file at the attacker's chosen path, 0600, with `wrote …/config.toml`
    printed -- a boundary escape past the very check `ProductionPaths._reject_symlink_ancestors`
    exists to make. `write_text` then created at `0666 & ~umask` and narrowed afterwards, the
    window `write_private_environment` opens `O_EXCL` at 0600 specifically to avoid. And the
    `exists()`-then-write pair was a check-then-act besides.

    One `os.open` answers all three, and `O_EXCL` carries most of it: `O_CREAT|O_EXCL` fails on
    an existing entry *of any kind*, a symlink included and a dangling one too, so "already
    there" becomes a syscall result rather than a guess and the link is refused rather than
    written through. `O_NOFOLLOW` is redundant beside it and kept as depth, not as the property
    -- a mutation check confirmed the test still passes without it, which is worth writing down
    rather than leaving a docstring claiming a flag is load-bearing when it is not. The mode is
    true at creation. The other two writers in this stage reached the same shape by different
    routes; this is the one that had not.
    """
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return f"kept the existing {path}"
    except OSError as error:
        raise ConfigError(f"cannot write {path}: {error}") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(render())
    return f"wrote {path}"


def _credential_summary(paths: ProductionPaths, arguments, interactive: bool) -> str:
    """Write the credential file, or report the one that is already private and readable."""
    try:
        return f"kept the existing {paths.require_private_environment()}"
    except ConfigError as refusal:
        # `exists()` was the check here, and it follows links and says nothing about type, size
        # or mode -- so a directory left at that path, or the zero-byte file a failed write used
        # to leave, was reported as a credential file being kept. The guard that already knows
        # what a usable credential file is answers instead.
        #
        # **And its answer is kept**, which the first version of this discarded with a bare
        # `pass`. A 0644 credential file made the guard say "must have mode 0600" -- precisely
        # the actionable sentence -- and the operator instead got "something already exists;
        # remove it first", about a file holding a token they may not be able to get again.
        # Only a genuinely absent file falls through to the writer.
        if paths.environment_path.exists() or paths.environment_path.is_symlink():
            raise ConfigError(f"the credential file cannot be used: {refusal}") from refusal
    secrets = onboarding_secrets(
        token_file=arguments.bot_token_file,
        owner_user_id=arguments.owner_user_id,
        owner_chat_id=arguments.owner_chat_id,
        environment=os.environ,
        ask=input if interactive else None,
        ask_secretly=getpass.getpass if interactive else None,
    )
    return f"wrote {paths.write_private_environment(secrets)}"


#: How long onboarding will wait for a service it just started, before reporting on it.
#: Two seconds total, in short steps: long enough for a fork plus an import, short enough that
#: nobody watching notices, and bounded so a service that will never start does not hold the
#: command open.
_SERVICE_START_ATTEMPTS = 8
_SERVICE_START_INTERVAL_SECONDS = 0.25


def _owner_id(value: str) -> int:
    """Parse an owner id without argparse echoing it back when it is not one.

    `type=int` looks harmless and is not: argparse renders a converter's failure as
    `invalid int value: '<what you typed>'`, and these two options sit in the same command as
    `--bot-token-file`. An operator who puts the token in the wrong one gets it printed back.
    Raising `ArgumentTypeError` makes the message this function's own, and this one names no
    value.

    Belt and braces with the parser's quoted-text redaction, deliberately: that redaction is a
    net under every message argparse can produce, and this is the one place the message can
    simply be right.
    """
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer (the value is not shown)") from None


def _run_command(argv: tuple[str, ...]) -> int:
    """Run one fixed local command and return its exit status, without a shell.

    The sibling of `_command_succeeds`, and separate from it because the two answer different
    questions: that one asks whether a probe passed and throws the status away, this one is what
    an installer needs when it has to report *how* something failed. Output is inherited rather
    than captured -- an operator watching `apt-get` or `systemctl` should see it work.
    """
    # The credential is stripped from the child's environment. `sudo` scrubs it anyway under the
    # default `env_reset`, but `brew` does not, and a Homebrew formula is arbitrary Ruby running
    # with whatever it inherited -- while the README's own unattended form puts the token in this
    # process's environment. Nothing this command runs has any use for it.
    environment = {
        name: value for name, value in os.environ.items() if name not in TELEGRAM_SECRET_VARIABLES
    }
    try:
        return subprocess.run(
            argv, check=False, stdin=subprocess.DEVNULL, timeout=600, env=environment
        ).returncode
    except (OSError, subprocess.SubprocessError):
        # Same shape as `_command_succeeds`: a command that could not start is a command that
        # failed, and the caller's own reporting is better than a traceback from here.
        return 1


def _package_manager_for_host() -> PackageManager:
    """Which package manager installs system dependencies here.

    A second platform question, deliberately not answered by re-reading the first. DEC-054 makes
    `SupervisorKind` a label that nothing may branch on, and the correlation it would express is
    false anyway: a systemd host may install with `dnf`, and Homebrew runs on Linux. Both
    questions are decided here, in a composition root, which is DEC-015's rule.
    """
    return PackageManager.HOMEBREW if sys.platform == "darwin" else PackageManager.APT


def _homebrew_is_installed() -> bool:
    """Whether `brew` is on this host's PATH, answered as a real bool.

    `render_remediation` takes this as a keyword with no default and reads it with `is not True`,
    so a probe that answered with a path or a string would render a `brew install` for a host
    with no `brew`. Coercing here is what makes that contract hold.
    """
    return shutil.which("brew") is not None


def _dependency_preflight(*, assume_yes: bool, interactive: bool) -> int:
    """Report what the host is missing, offer the exact fix, and re-probe rather than assume.

    The re-probe is the point of the last three lines. `InstallAttempt.resolved` means the
    installer reported success, which is not the same claim as "the dependency is there" -- brew
    exits 0 for a formula that was already present, and an installer can succeed at installing
    something other than what was asked for. So the answer onboarding acts on comes from looking
    again, not from an exit status.
    """
    probe = _dependency_probe()
    missing = [status.name for status in probe if status.state == MISSING]
    for status in probe:
        detail = status.version or status.note or "no version reported"
        print(f"  {status.name}: {status.state} ({detail})")
    if not missing:
        return 0
    remediation = render_remediation(
        missing,
        package_manager=_package_manager_for_host(),
        homebrew_installed=_homebrew_is_installed(),
    )
    attempt = confirm_and_install(
        remediation,
        announce=lambda line: print(f"  to install what is missing: {line}"),
        confirm=_ask_to_confirm if interactive else None,
        run=_run_command,
        assume_yes=assume_yes,
    )
    if not attempt.resolved:
        print(f"  not installed ({attempt.outcome}); run the command above and re-run onboarding")
        return 1
    still_missing = [status.name for status in _dependency_probe() if status.state == MISSING]
    if still_missing:
        print(f"  the installer reported success but {', '.join(still_missing)} is still missing")
        return 1
    return 0


def _dependency_probe():
    """Probe this host's required executables, with the two effects supplied from here."""
    return probe_dependencies(
        resolve=lambda name: _resolved_executable(name),
        run_version=lambda argv: (
            subprocess.run(
                argv,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=5,
            ).stdout
        ),
    )


def _resolved_executable(name: str) -> Path | None:
    resolved = shutil.which(name)
    return Path(resolved) if resolved is not None else None


def _ask_to_confirm(prompt: str) -> bool:
    """Ask a yes/no question and answer it with a **bool**, never with what was typed.

    `confirm_and_install` takes consent as `is True` rather than as truthiness, because a
    `confirm` that returned the operator's own text would have installed on "n" -- every plain
    refusal is a non-empty string. This is the adapter that makes that contract hold: the typing
    happens here, and only `y`/`yes` becomes `True`.
    """
    try:
        answer = input(f"{prompt} [y/N] ")
    except EOFError:
        # A closed stdin is not a yes. Reached when a run that looked interactive turns out not
        # to be -- a pipe, a CI shell -- and treating the exception as anything but a refusal
        # would install without a human present.
        return False
    return answer.strip().lower() in ("y", "yes")


def onboarding_secrets(
    *,
    token_file: Path | None,
    owner_user_id: int | None,
    owner_chat_id: int | None,
    environment: Mapping[str, str],
    ask: Callable[[str], str] | None,
    ask_secretly: Callable[[str], str] | None,
) -> TelegramSecrets:
    """Resolve the three credentials from a flag, the environment, or a prompt -- in that order.

    **There is deliberately no `--bot-token VALUE`, and its absence is the security decision in
    this function.** On Linux `/proc/<pid>/cmdline` is world-readable, so a token passed as an
    argument is disclosed to every process on the host for as long as onboarding runs, and it
    lands in the operator's shell history besides. That is precisely the exposure the 0600 file
    exists to prevent, arriving one command earlier. `--bot-token-file` names a path instead --
    the value never becomes argv -- and a run driven by a supervisor or a script supplies all
    three through the environment, which `load_secrets` already reads for `serve`.

    The precedence is flag, then environment, then prompt, because a flag is what the operator
    typed *this time* while an exported variable may be a rotation ago. A missing value with no
    terminal to ask is a refusal naming the variable to supply, never a prompt into a closed
    stdin: an unattended run that blocks forever on an invisible `getpass` is the worst of the
    available failures, because nothing on screen says what it is waiting for.

    **Nothing here renders the token.** It is read through `ask_secretly` (a `getpass`, wired by
    the caller) and never through `ask`, and every error raised below names a *variable*, never a
    value -- the error paths being where a credential is most likely to be printed by accident,
    since they are the paths a fixture is least likely to cover.

    **Why this policy lives in the composition root rather than in `application/`, where Stage 1
    put the dependency policy.** A Tier-2 review was right that the shape is the same -- a
    precedence rule with its effects injected -- and would be right that `application/` is where
    such a rule belongs, except that this one cannot go there: it is built out of
    `TELEGRAM_SECRET_VARIABLES`, `TelegramSecrets`, `load_secrets` and `ConfigError`, every one of
    them from `remote_agents.config`, which DEC-015 forbids `application/` to import and
    `tests/architecture/check_imports.py` enforces. Moving it would mean a second copy of the
    variable names in another layer, which is the shadow-copy this project has already been
    bitten by twice. `describe_schema_drift` sits where it does for exactly this reason, and this
    is the same trade recorded a second time so the next reader does not re-open it.
    """
    names = TELEGRAM_SECRET_VARIABLES
    token = _first_supplied(
        _token_from_file(token_file),
        environment.get(names[0]),
        lambda: None if ask_secretly is None else ask_secretly("Telegram bot token: "),
    )
    user_id = _first_supplied(
        None if owner_user_id is None else str(owner_user_id),
        environment.get(names[1]),
        lambda: None if ask is None else ask("Owner user id: "),
    )
    chat_id = _first_supplied(
        None if owner_chat_id is None else str(owner_chat_id),
        environment.get(names[2]),
        lambda: None if ask is None else ask("Owner chat id: "),
    )
    resolved = dict(zip(names, (token, user_id, chat_id), strict=True))
    missing = [name for name, value in resolved.items() if not value]
    if missing:
        raise ConfigError(f"missing required values: {', '.join(missing)}")
    secrets = load_secrets(resolved)
    if secrets is None:
        # `load_secrets(production=True)` raises rather than returning None, so this is
        # unreachable -- and it was an `assert`, which `python -O` deletes. An unreachable branch
        # that a flag can turn into `return None` from a function annotated to return a value is
        # not the place to save two lines.
        raise ConfigError("the Telegram credentials could not be resolved")
    return secrets


def _first_supplied(
    flag: str | None, injected: str | None, asked: Callable[[], str | None]
) -> str | None:
    """Take the first source that answered, asking only if neither earlier one did.

    A callable for the third, so a prompt is never raised for a value that was already supplied
    -- which is what makes the fully-non-interactive path provably silent rather than silent by
    luck.
    """
    for value in (flag, injected):
        if value:
            return value.strip()
    answered = asked()
    return None if answered is None else answered.strip()


def _token_from_file(path: Path | None) -> str | None:
    """Read a token out of a file the operator named, so the value never becomes argv."""
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        # **The path is rendered only when it exists.** A path that does not is overwhelmingly
        # likely to *be* the token -- someone typed `--bot-token-file <the token>`, or reached
        # for `--bot-token` and had it abbreviated into this one -- so naming it is the leak
        # rather than the diagnosis. When the file does exist the path is a genuine path and
        # printing it is what makes the error actionable.
        if path.exists():
            raise ConfigError(f"cannot read the bot token file {path}") from error
        raise ConfigError("--bot-token-file names no such file (the value is not shown)") from error


#: Where an upgrade looks for releases, matching `scripts/install.sh`'s own default.
#:
#: Stated here as well as in the script because the two must agree and nothing else makes them:
#: the script is not packaged into the wheel, so an installed copy cannot read it. A test pins
#: that they match rather than trusting the comment.
DEFAULT_REPOSITORY = "https://github.com/sureserverman/remote-agents"

#: How long the release check may take before it is abandoned.
#:
#: `doctor` runs at the end of every `onboard`, including unattended ones on hosts with no route
#: out, so this call must be incapable of hanging the one command an operator runs to find out
#: whether their install worked. Three seconds is long enough for a `ls-remote` on a working
#: connection and short enough that a dead one is a pause rather than a stall. Failure is always
#: "unknown", never an error.
_RELEASE_CHECK_TIMEOUT = 3


def _remote_release_tags(repository: str, timeout: int = _RELEASE_CHECK_TIMEOUT) -> tuple[str, ...]:
    """Ask a remote which release tags it carries, answering empty on any failure.

    `git ls-remote --tags` rather than a GitHub API call: it needs no token, no JSON parsing and
    no vendor-specific endpoint, and it works against any mirror an operator points
    `REMOTE_AGENTS_REPOSITORY` at. The `^{}` peeled refs annotated tags produce are left in --
    `newest_release` drops everything it cannot parse, so filtering twice would be two places to
    keep in step.
    """
    if shutil.which("git") is None:
        return ()
    try:
        completed = subprocess.run(
            ("git", "ls-remote", "--tags", repository),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if completed.returncode != 0:
        return ()
    return tuple(
        line.rsplit("/", 1)[-1] for line in completed.stdout.splitlines() if "refs/tags/" in line
    )


def _release_state(repository: str = DEFAULT_REPOSITORY) -> dict[str, object]:
    """What `doctor` prints about this install's version against the newest published one."""
    tags = _remote_release_tags(repository)
    latest = newest_release(tags)
    return release_status(
        __version__, latest, None if latest is not None else "release_list_unavailable"
    )


def _run_upgrade(arguments) -> int:
    """Re-install this tool at a newer pinned tag, then let the daemon pick it up.

    **This is the verb the pin took away.** `uv tool upgrade` re-resolves the requirement the
    install recorded, and that requirement is an exact git rev, so it resolves to itself and
    reports `Nothing to upgrade` having done nothing -- correct behaviour, exit 0, and
    indistinguishable to a reader from being up to date. The pin is worth keeping: an install
    that moved whenever the default branch moved would be a credential-holding daemon changing
    under a host with live agent sessions on it. What was not worth keeping was having no
    command that does the obvious thing.

    The safety properties of `scripts/install.sh` are preserved rather than re-derived: the
    target must be tag-shaped (`is_release_tag`, which is that script's `v[0-9]*` rule stated
    exactly), the repository and version are printed before anything is installed, and the
    install itself is the same `uv tool install` invocation. What is deliberately *not* carried
    over is the script's uv bootstrap, because reaching this command means uv already installed
    this tool.

    `--check` reports and changes nothing, which is what makes this safe to run from a habit.
    """
    repository = arguments.repository
    if arguments.version is not None:
        target = arguments.version
        if not is_release_tag(target):
            print(
                f"'{target}' is not a release tag. This tool installs from pinned tags "
                "so that two hosts bootstrapped an hour apart run the same code.",
                file=sys.stderr,
            )
            return 2
    else:
        latest = newest_release(_remote_release_tags(repository, timeout=15))
        if latest is None:
            print(
                f"could not read the release tags of {repository}. "
                "Pass --version to name one explicitly.",
                file=sys.stderr,
            )
            return 1
        target = latest

    print(f"installed: {__version__}")
    print(f"newest:    {target}")
    if not upgrade_available(__version__, target) and arguments.version is None:
        print("already up to date.")
        return 0
    if arguments.check:
        print("an upgrade is available; re-run without --check to take it.")
        return 0

    print(f"Installing remote-agents {target} from {repository}")
    installed = _run_command(
        (
            "uv",
            "tool",
            "install",
            "--managed-python",
            "--force",
            f"remote-agents @ git+{repository}@{target}",
        )
    )
    if installed != 0:
        print("the install failed; the daemon has not been touched.", file=sys.stderr)
        return 1
    # The daemon is registered against a path, and an upgrade that relocates the executable
    # leaves the old one named in the unit. Re-running onboarding is what rewrites it, and it is
    # idempotent when nothing moved -- the same reason `scripts/install.sh` ends this way.
    print("Re-registering the daemon so it picks up the new code...")
    return _run_command((_installed_executable(), "onboard", "--install-daemon"))


def _installed_executable() -> str:
    """The console script uv just installed, asked of uv rather than assumed.

    `sys.executable` is *this* process's interpreter, which is the copy being replaced. Asking uv
    where the entry point landed is the same question `scripts/install.sh` answers with
    `uv tool dir --bin`, and for the same reason: `~/.local/bin` is not on every login shell's
    PATH and is absent from macOS's `_PATH_STDPATH` outright.
    """
    try:
        completed = subprocess.run(
            ("uv", "tool", "dir", "--bin"),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "remote-agents"
    directory = completed.stdout.strip()
    candidate = Path(directory) / "remote-agents" if directory else None
    return str(candidate) if candidate is not None and candidate.is_file() else "remote-agents"


def detected_config(home: Path, dev_root: Path | None = None) -> str:
    """Render this host's configuration from the one thing onboarding actually knows: its home.

    The composition root is where this belongs, and not by default. `render_config` holds the
    schema and refuses an incomplete set of keys; `ProductionPaths` holds the private tree and
    where the database goes. This function is the only place that knows *both*, plus the two
    paths that are neither -- the operator's dev tree and the projects registry, which live in
    their home but outside the boundary `ProductionPaths` declares itself the owner of. DEC-015
    puts exactly that kind of joining here.

    `~/dev` and `~/.claude/projects-registry.yaml` are the shipped example's two paths with the
    hardcoded home taken out, so an operator whose layout already matches the example gets the
    file they would have written. An operator whose does not gets a config that loads and a
    `doctor` that tells them the registry is unavailable, which is the honest answer for a host
    that has no registry yet -- and is a different sentence from the crash a copied example
    produces at the first `serve`.

    Public, unusually for this module, because the onboarding test has to read what would be
    written without writing it. The alternative was asserting on a file, which would have made
    every case in that test a filesystem case.
    """
    paths = ProductionPaths.for_home(home)
    return render_config(
        dev_root=home / "dev" if dev_root is None else dev_root,
        registry_path=home / ".claude" / "projects-registry.yaml",
        database_path=paths.database_path,
    )


def _host_platform() -> dict[str, object]:
    """Which machine this is, for a report someone else has to read.

    `_supervisor_for_host` below answers a *decision* -- which supervisor owns this host's user
    services -- and answers it from `sys.platform`, deliberately, because a Mac with neither
    tool installed is still a launchd host. This answers a different question: what to write
    down. The two are kept apart on purpose. Collapsing them would tempt a later reader to
    branch on this dict, and DEC-054 makes the supervisor a label nothing may branch on for
    exactly that reason.

    `machine` earns its place rather than padding the dict. The launchd adapter derives its
    plist `PATH` from `brew --prefix`, which is `/opt/homebrew` on Apple Silicon and
    `/usr/local` on Intel; without the architecture in the report, a derived value that came
    out wrong cannot be checked against the host that derived it. `release` is the Darwin
    kernel version on a Mac rather than the marketing version -- the honest thing
    `platform.release()` returns on both platforms, and uniform across them, which a bug report
    can act on where a field meaning two different things could not.
    """
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
    }


def _supervisor_for_host() -> ServiceSupervisor:
    """Which supervisor actually runs this service here.

    The one place the platform is decided. DEC-001 puts the *difference* in the adapters and
    DEC-015 puts the *choosing* in a composition root, which is this file -- so `doctor` and
    everything downstream ask the port a question and never learn which supervisor answered
    it, except to report the name.

    `sys.platform` rather than probing for an installed binary: the question is which
    supervisor owns this host's user services, and a Mac with neither tool installed is still
    a launchd host. Probing would answer "systemd" there the moment someone had a stray
    `systemctl` on their PATH.
    """
    try:
        # `Path.home()` is written here, once, and nowhere else. It used to be the adapters'
        # own default, which meant every construction anywhere -- including a contract test's --
        # silently described this machine, and those adapters name the files removal deletes.
        # Naming it at the one composition point that is entitled to it is the point of the
        # argument being required.
        if sys.platform == "darwin":
            return LaunchdSupervisor(home=Path.home())
        return SystemdSupervisor(home=Path.home())
    except ValueError as error:
        # The adapters refuse a home or interpreter they cannot render faithfully -- a colon
        # that would split the plist PATH, a control character that would inject a unit
        # directive. Those are real refusals and must not be swallowed, but they reach here
        # from `serve` and the local surface too, neither of which is installing anything, and
        # a bare ValueError there is a traceback rather than a diagnosis. `ConfigError` is the
        # handled path every other bad-configuration answer already travels; the adapters
        # cannot raise it themselves because ARCH-02 forbids them importing `config`.
        raise ConfigError(f"this host cannot be described to its service supervisor: {error}")
