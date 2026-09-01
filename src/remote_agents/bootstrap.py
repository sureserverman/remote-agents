"""Composition root for the private Telegram control-plane service."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from remote_agents.adapters.agents.registry import (
    HookInstallError,
    default_settings_path,
    install_agent_hooks,
    remove_agent_hooks,
)
from remote_agents.adapters.projects.discovery import discover_projects
from remote_agents.adapters.projects.registry import load_registry
from remote_agents.adapters.sqlite.database import (
    database_is_ready,
    leased_connection,
    open_database,
    restore_database,
)
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.telegram.service import (
    PrivateBotBoundary,
    audit_owner_metadata,
    run_private_bot,
)
from remote_agents.adapters.tmux.codec import switch_client_argv
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import (
    probe_profiles,
)
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner
from remote_agents.adapters.tui import PANE_NAMES

if TYPE_CHECKING:
    # Annotations only. The terminal's own modules are imported inside the functions that
    # need them, so `serve` never loads the terminal library and a failure in it cannot
    # reach the bot — naming them here would undo that.
    from remote_agents.adapters.tui.context import TuiContext
    from remote_agents.adapters.tui.model import AttachRequest
from remote_agents.agent_event import spool_from_stdin
from remote_agents.application.doctor import (
    credential_file_report,
    production_doctor,
    profile_doctor,
)
from remote_agents.application.errors import ProjectCreationError
from remote_agents.application.project_admin import CreateProjectCommand
from remote_agents.composition.backend import (
    ProjectCatalogueProvider,
    _project_creator,
)
from remote_agents.composition.onboarding import (
    DEFAULT_REPOSITORY,
    _host_platform,
    _onboard,
    _owner_id,
    _release_state,
    _run_upgrade,
    _supervisor_for_host,
)
from remote_agents.composition.service import (
    _serve_with_reconciliation,
)
from remote_agents.composition.telegram import _private_boundary
from remote_agents.composition.tui import (
    _console_composer,
    _resolve_profile_executable,
    local_context,
)
from remote_agents.config import (
    TELEGRAM_SECRET_VARIABLES,
    ConfigError,
    TelegramSecrets,
    describe_schema_drift,
    load_config,
    load_secrets,
)
from remote_agents.domain.models import SessionId
from remote_agents.domain.profiles import closed_profiles
from remote_agents.ports.argv_text import (
    NonEchoingArgumentParser,
    refuse_a_credential_shaped_value,
)
from remote_agents.ports.service_supervisor import SupervisorKind
from remote_agents.production import ProductionPaths

_LOG = logging.getLogger(__name__)
_RECONCILE_INTERVAL_SECONDS = 60.0


def main(
    argv: list[str] | None = None,
    *,
    serve_runner: Callable[[TelegramSecrets, PrivateBotBoundary], Awaitable[None]] = (
        run_private_bot
    ),
) -> int:
    """Run the current composition-root command-line interface."""
    parser = NonEchoingArgumentParser(
        prog="remote-agents",
        description="Private Telegram control plane for local agent sessions.",
    )
    subcommands = parser.add_subparsers(dest="command")
    doctor_parser = subcommands.add_parser("doctor")
    doctor_parser.add_argument("--config", type=Path)
    doctor_parser.add_argument("--fake-terminal", action="store_true")
    doctor_parser.add_argument("--profiles", action="store_true")
    doctor_parser.add_argument("--json", action="store_true")
    # BL-030: the append-only lifecycle history has been written since migration 1 with
    # nothing able to read it back, so the runbook described an audit trail an operator could
    # only reach by opening sqlite by hand. It lands on `doctor` rather than on the bot or the
    # TUI because it is a read-only diagnostic, which is what `doctor` already is -- and
    # because adding a row to either surface would move the parity contract for a report
    # neither surface has a use for mid-session.
    doctor_parser.add_argument("--history", type=str, default=None)
    restore_parser = subcommands.add_parser("restore-database")
    restore_parser.add_argument("--database", type=Path, required=True)
    restore_parser.add_argument("--backup", type=Path)
    serve_parser = subcommands.add_parser("serve")
    serve_parser.add_argument("--config", type=Path, required=True)
    telegram_audit_parser = subcommands.add_parser("telegram-ui-audit")
    telegram_audit_parser.add_argument("--json", action="store_true")
    add_project_parser = subcommands.add_parser("add-project")
    add_project_parser.add_argument("--config", type=Path)
    add_project_parser.add_argument("--area", required=True)
    add_project_parser.add_argument("--name", required=True)
    tui_parser = subcommands.add_parser("tui")
    tui_parser.add_argument("--config", type=Path)
    # One process per tmux pane: a Textual app owns a terminal, and the console is three
    # panes side by side. `choices` is what refuses an unknown name — before anything is
    # composed, rather than after a database is opened for a surface that does not exist.
    pane_parser = subcommands.add_parser("pane")
    pane_parser.add_argument("name", choices=sorted(PANE_NAMES))
    pane_parser.add_argument("--config", type=Path)
    # What the console's projects key runs. It exists because a tmux key cannot do this
    # itself: tmux can select a window, but it cannot read our pane marks and work out which
    # exchange brings the surface home. Not a surface — it arranges panes and exits.
    console_parser = subcommands.add_parser("console")
    console_parser.add_argument("action", choices=("projects",))
    # A one-time repair for sessions launched before identity moved to the pane (DEC-038).
    # They stayed manageable but gained no pane to exchange, so the console could not show
    # them. Explicit rather than automatic: it writes onto a running agent's pane.
    subcommands.add_parser("upgrade-sessions")
    agent_event_parser = subcommands.add_parser("agent-event")
    agent_event_parser.add_argument("--activity-dir", type=Path)
    agent_event_parser.add_argument("--provider", choices=("claude", "codex"), default="claude")
    # `allow_abbrev=False` is load-bearing, not tidiness. argparse accepts any unambiguous
    # prefix by default, so `--bot-token` -- the obvious name, the one an operator reaches for
    # first -- was silently accepted as an abbreviation of `--bot-token-file`, which put a
    # credential in argv and then printed it in the "cannot read the bot token file …" error.
    # The whole point of having no such flag was defeated by argparse inventing one.
    onboard_parser = subcommands.add_parser("onboard", allow_abbrev=False)
    # Mutually exclusive, because the two are opposite intentions and the handler has to check
    # one of them first: `--install-daemon --remove` silently removed and never installed, with
    # nothing said. argparse refuses the pair before anything is composed.
    onboard_daemon = onboard_parser.add_mutually_exclusive_group()
    onboard_daemon.add_argument("--install-daemon", action="store_true")
    onboard_daemon.add_argument("--remove", action="store_true")
    # In the same group, because asking is the opposite intention from acting and the handler
    # has to check one of them first -- whichever lost would be silently ignored, which is the
    # defect that put `--install-daemon` and `--remove` in a group to begin with.
    onboard_daemon.add_argument("--print-daemon-path", action="store_true")
    onboard_parser.add_argument("--yes", action="store_true")
    # A path, never a value: `/proc/<pid>/cmdline` is world-readable on Linux, so a token given
    # as an argument is disclosed to every process on the host and kept in shell history.
    onboard_parser.add_argument("--bot-token-file", type=Path)
    # Declared **so that it can be refused**, which is the only way to refuse it quietly:
    # argparse's own "unrecognized arguments: --bot-token <value>" prints the value too, so
    # leaving the name undefined is not the same as making it unusable. `SUPPRESS` keeps it out
    # of `--help`, where advertising it would invite the mistake this exists to catch.
    onboard_parser.add_argument(
        "--bot-token", dest="rejected_token", default=None, help=argparse.SUPPRESS
    )
    onboard_parser.add_argument("--owner-user-id", type=_owner_id)
    onboard_parser.add_argument("--dev-root", type=Path)
    onboard_parser.add_argument("--owner-chat-id", type=_owner_id)
    # `upgrade`, because `uv tool upgrade` cannot do it: the install pins an exact git rev, so
    # uv re-resolves it to itself and reports `Nothing to upgrade` while doing nothing. The pin
    # stays -- a daemon that moved whenever the default branch moved is worse -- so the verb it
    # took away is supplied here instead.
    upgrade_parser = subcommands.add_parser("upgrade")
    upgrade_parser.add_argument("--version", type=str, default=None)
    upgrade_parser.add_argument("--repository", type=str, default=DEFAULT_REPOSITORY)
    upgrade_parser.add_argument("--check", action="store_true")
    install_hooks_parser = subcommands.add_parser("install-agent-hooks")
    install_hooks_parser.add_argument("--provider", choices=("claude", "codex"), default="claude")
    install_hooks_parser.add_argument("--settings", type=Path)
    install_hooks_parser.add_argument("--activity-dir", type=Path)
    install_hooks_parser.add_argument("--remove", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.command == "agent-event":
        # Delegated rather than implemented here: `__main__` routes the installed hook command
        # straight to that module without importing this one, and two copies of a path that
        # promises never to raise would eventually stop agreeing about how it does that.
        return spool_from_stdin(arguments.activity_dir, provider=arguments.provider)
    if arguments.command == "upgrade":
        return _run_upgrade(arguments)
    if arguments.command == "install-agent-hooks":
        # Wrapped below rather than at each call: `refuse_a_credential_shaped_value` raises
        # `ValueError`, and this branch already turns a `HookInstallError` into a printed line.
        # --settings names the file to operate on, and --activity-dir the spool the installed
        # command will write to. Both default to the owner's real ones and exist so that the
        # live drill can drive a real agent end to end without going near either.
        try:
            for option, given in (
                ("--settings", arguments.settings),
                ("--activity-dir", arguments.activity_dir),
            ):
                if given is not None:
                    refuse_a_credential_shaped_value(option, str(given))
            settings_path = arguments.settings or default_settings_path(
                Path.home(), provider=arguments.provider
            )
            outcome = (
                remove_agent_hooks(settings_path, provider=arguments.provider)
                if arguments.remove
                else install_agent_hooks(
                    settings_path,
                    activity_directory=arguments.activity_dir,
                    provider=arguments.provider,
                )
            )
        except (HookInstallError, ValueError) as error:
            print(error, file=sys.stderr)
            return 1
        print(outcome.summary)
        return 0
    if arguments.command == "onboard":
        try:
            return _onboard(arguments)
        except (ConfigError, ValueError) as error:
            print(error, file=sys.stderr)
            return 1
    if arguments.command == "doctor":
        if arguments.profiles:
            result = profile_doctor(probe_profiles(closed_profiles()))
            print(json.dumps(result, sort_keys=True) if arguments.json else result)
            return 0
        if arguments.history is not None:
            return _print_session_history(arguments)
        paths = ProductionPaths.for_home(Path.home())
        config_path = arguments.config or paths.config_path
        # `add-project` and `tui` have both caught ConfigError here for as long as they have
        # existed; `doctor` did not, which meant the one command an operator runs *before*
        # trusting a deploy raised a traceback on exactly the input it exists to diagnose
        # (BL-029). Diagnose first, then decide whether there is anything left to check.
        drift = describe_schema_drift(config_path)
        if not drift["readable"]:
            # Report the one thing that was actually observed, and say plainly that nothing
            # else was. The obvious shape here is to call `production_doctor` with every
            # component set False, and it is wrong: `core_ready=False` renders as
            # `registry_unavailable`, `tmux_ready=False` as `tmux_unavailable`, and neither
            # was ever probed -- the registry path and the database path are read *out of*
            # the config that would not load. That report would assert six failures nobody
            # looked for, on a host where tmux may be perfectly fine, and send an operator
            # chasing five phantoms behind one real fault.
            report = {
                "healthy": False,
                "config": drift,
                "components": {},
                "checked": False,
                # The one thing still knowable when the config is not: it is read from the
                # running process, never from the file that failed. Every other field here is
                # withheld precisely because it would have to come out of that file.
                "platform": _host_platform(),
            }
            print(json.dumps(report, sort_keys=True) if arguments.json else report)
            return 1
        # Guarded even though `describe_schema_drift` just proved the file loads, which is the
        # try/except the plan asked for and the check-then-act above does not replace. The two
        # calls are two separate reads, so an operator editing the deployed config in the
        # window between them would land the very traceback BL-029 exists to remove -- and
        # editing that file is exactly what someone running `doctor` is about to do.
        try:
            config = load_config(config_path)
        except ConfigError as error:
            print(error, file=sys.stderr)
            return 1
        result = _doctor_report(paths, config, drift)
        print(json.dumps(result, sort_keys=True) if arguments.json else result)
    if arguments.command == "restore-database":
        restore_database(arguments.database, arguments.backup)
        print("database restored")
    if arguments.command == "telegram-ui-audit":
        paths = ProductionPaths.for_home(Path.home())
        secrets = _load_private_telegram_secrets(paths)
        result = asyncio.run(audit_owner_metadata(secrets))
        print(json.dumps(result, sort_keys=True) if arguments.json else result)
        return 0 if result["healthy"] else 1
    if arguments.command == "add-project":
        paths = ProductionPaths.for_home(Path.home())
        try:
            config = load_config(arguments.config or paths.config_path)
            # Observed BEFORE the create, because the create is what brings it into existence.
            registry_was_absent = not Path(config.registry_path).exists()
            created = _project_creator(config).create(
                CreateProjectCommand(arguments.area.strip(), arguments.name.strip())
            )
        except (ConfigError, ProjectCreationError) as error:
            print(error, file=sys.stderr)
            return 1
        if registry_was_absent:
            # **Creating the registry is allowed (DEC-060); creating it SILENTLY is not.**
            #
            # Auto-creation turns one specific misconfiguration into a silent success. A
            # `registry_path` that is typo'd, points at an unmounted volume, or carries a home
            # baked in on another machine -- which `config/remote-agents.example.toml` did, with
            # a `/home/...` path that exists on no Mac -- used to surface as
            # `core: registry_unavailable` and get investigated. Now it produces a brand-new
            # empty registry at the wrong place, a success, and a green `doctor`, while the real
            # registry sits untouched and unused.
            #
            # The dead end this replaced at least complained. Saying so restores the signal that
            # auto-creation removes, at the cost of one line, and it goes to stderr so stdout
            # stays exactly the created path for anything parsing it.
            print(
                f"note: created a new projects registry at {config.registry_path}\n"
                f"      if your projects are registered somewhere else, check `registry_path` in "
                f"your config --\n"
                f"      a wrong path creates an empty registry here instead of using yours.",
                file=sys.stderr,
            )
        print(created.path)
        return 0
    if arguments.command == "tui":
        from remote_agents.adapters.tui.app import run_local_terminal

        return _run_surface(arguments.config, run_local_terminal, "the local terminal surface")
    if arguments.command == "pane":
        return _enter_pane(arguments.name, arguments.config)
    if arguments.command == "console":
        return _console_arrange(arguments.action)
    if arguments.command == "upgrade-sessions":
        return _upgrade_sessions()
    if arguments.command == "serve":
        # **Deliberately unguarded, and the leak it was blamed for is closed elsewhere.** A
        # reviewer found `serve --config=<token>` printing `FileNotFoundError: … '<token>'`
        # above the redacted message, and the obvious repair was a handler here. The actual
        # cause was the *exception chain*: `raise ... from error` prints the cause above the
        # message, so redacting the message while the traceback repeats it is not redacting.
        # `config._unreadable`'s raise breaks the chain for a path that does not exist, which
        # fixes it for every reader rather than for this one.
        #
        # A handler here would also change what `serve` promises: it raises `ConfigError` today,
        # `tests/integration/test_live_service.py` pins that it does so *after* closing its
        # database, and swallowing it into an exit status is a contract change this plan has no
        # business making on the way past.
        return _serve(arguments, serve_runner)
    if arguments.command is None:
        # The bare name was unclaimed — no arguments fell through every branch above and
        # exited 0 silently — so this claims it for the one thing a bare invocation can
        # mean: enter the console.
        return _enter_console()
    return 0


def _serve(arguments, serve_runner) -> int:
    """Run the installed service. Extracted so `main` can guard it like every other command."""
    paths = ProductionPaths.for_home(Path.home())
    config = _private_state_config(arguments.config, paths)
    wants_unit_directory = _supervisor_for_host().kind is SupervisorKind.SYSTEMD
    paths.ensure_directories(include_unit_directory=wants_unit_directory)
    paths.require_private_environment()
    connection = paths.open_database(
        open_database, migrations=MIGRATIONS, include_unit_directory=wants_unit_directory
    )
    # Resolved **once** and threaded into both consumers. The duplicate call this
    # replaces was harmless while the only source was `os.environ`, which cannot change
    # inside a running process: two reads were the same read. The private-file fallback is
    # a file on disk, so two independent resolutions can straddle a credential rotation
    # and pair a new bot token with a stale owner id -- and the owner id is what seeds the
    # ACL. Making it a parameter is what stops the pair coming apart.
    try:
        # Inside the `try`, not above it: resolution raises on a partial environment or on
        # a credential file that fails its guard, and the database is already open by then.
        # Above the `try`, that exception skips `finally` and leaves the connection open.
        serve_secrets = _resolve_serve_secrets(paths)
        asyncio.run(
            _serve_with_reconciliation(
                serve_secrets,
                _private_boundary(config, connection, paths, serve_secrets),
                serve_runner,
                _RECONCILE_INTERVAL_SECONDS,
                config.activity_poll_seconds,
            )
        )
    finally:
        connection.close()
    return 0


def _enter_console(
    *,
    environment: Mapping[str, str] | None = None,
    ensure_console: Callable[[], Awaitable[bool]] | None = None,
    exec_argv: Callable[[str, tuple[str, ...]], None] = os.execvp,
) -> int:
    """Enter the console: ensure it exists and become its client, honoring the hosting.

    The bare invocation's whole meaning. A client already on our server is told it is
    already there, and told what the one root key does — this line said "F12 returns to the
    dashboard" until Sub-plan 3, which was the tab model's answer and named a surface the
    console does not run; a foreign tmux client gets the command printed rather than a nested
    client; a bare shell ensures the console — one window of three panes, running
    `remote-agents pane projects|sessions|feed` — and execs the attach, exactly the handoff
    shape a ready launch has always used. An exec that cannot happen prints the same command
    and exits non-zero, so the console is never lost behind a silent failure.

    "Window 0 running `remote-agents tui`" until Sub-plan 3, which is what a single-pane
    console was. `_console_composer` supplies a command per pane now, so that is no longer
    the shape this builds.
    """
    from remote_agents.adapters.tmux.codec import console_attach_argv
    from remote_agents.adapters.tui.attach import HostingMode, hosting_mode

    values = os.environ if environment is None else environment
    mode = hosting_mode(values)
    command = " ".join(console_attach_argv())
    if mode is HostingMode.CONSOLE:
        print("Already in the console. F12 shows the projects pane.")
        return 0
    if mode is HostingMode.FOREIGN:
        print(
            "Already inside another tmux. Detach first and run `remote-agents`, or attach "
            f"from a new terminal with:\n{command}"
        )
        return 0
    if ensure_console is None:
        ensure_console = _console_composer().ensure
    if not asyncio.run(ensure_console()):
        print(
            "The console could not be prepared. Check tmux on this host, or run: "
            "remote-agents doctor",
            file=sys.stderr,
        )
        return 1
    argv = console_attach_argv()
    try:
        exec_argv(argv[0], argv)
    except OSError:
        print(f"Could not attach automatically. Attach with:\n{command}", file=sys.stderr)
        return 1
    return 0


def _console_arrange(action: str) -> int:
    """Rearrange the console's panes and exit — the operator's route back from an agent.

    Deliberately not a surface: it holds no database handle, renders nothing, and its whole
    life is one exchange. It is presentation like everything else the composer does, so a
    failure here is a log line and a non-zero exit, never a session's problem (DEC-006).
    """
    if action != "projects":  # pragma: no cover - argparse `choices` is the real guard
        print(f"unknown console action: {action}", file=sys.stderr)
        return 1
    asyncio.run(_console_composer().show_projects())
    return 0


def _upgrade_sessions() -> int:
    """Give every session still marked under the old scheme an identity on its own pane.

    Says what it did, including when there was nothing to do, because "nothing happened" is
    the failure mode this whole repair exists to end.
    """
    gateway = TmuxGateway("remote-agents", AsyncTmuxRunner())
    try:
        upgraded = asyncio.run(gateway.upgrade_pane_identity())
    except Exception as error:  # noqa: BLE001 - reported, never a traceback at the terminal
        print(f"The sessions could not be upgraded: {error}", file=sys.stderr)
        return 1
    if not upgraded:
        print("Every managed session already carries its identity on its own pane.")
        return 0
    for session_id in upgraded:
        print(f"upgraded ra-{session_id}")
    print(
        f"{len(upgraded)} session(s) upgraded. The console can show them now — no restart "
        "needed, and nothing was interrupted."
    )
    return 0


def _run_surface(
    config_path: Path | None,
    runner: Callable[[TuiContext], AttachRequest | None],
    label: str,
) -> int:
    """Compose the local surface over the private store, run it, and honor what it hands back.

    One body, two entry points — `tui`'s combined dashboard and `pane`'s single-pane
    surface — because everything except *which surface runs* is identical: the same
    confinement check, the same migration, the same lease, the same failure message, the
    same attach handoff. Written twice, the copies had already started to drift within one
    stage, which is what a Tier-2 review caught.

    Migrations and the pre-migration backup run once, on a real connection that is closed
    before the surface starts; the surface itself works over a per-operation lease and holds
    no database handle between operations (DEC-035). That is the stated answer to the
    question DEC-023 recorded as open, superseded at the console-surface plan's close-out:
    the surface may now be long-lived beside attached sessions, and what keeps DEC-005's
    two-writer story simple is no longer "the terminal exec'd away", it is that the
    terminal's handle exists only inside a single store operation. The README states the
    reworded guarantee.

    Three pane processes start together and each runs this. They serialize on SQLite's write
    lock under the busy timeout `open_database` sets, and a migration already applied is a
    version read — so the concurrency is the two-writer story the bot and the surface already
    told, at one more writer.
    """
    from remote_agents.adapters.tui.attach import attach_to

    paths = ProductionPaths.for_home(Path.home())
    try:
        config = _private_state_config(config_path or paths.config_path, paths)
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 1
    wants_unit_directory = _supervisor_for_host().kind is SupervisorKind.SYSTEMD
    paths.ensure_directories(include_unit_directory=wants_unit_directory)
    paths.open_database(
        open_database, migrations=MIGRATIONS, include_unit_directory=wants_unit_directory
    ).close()
    connection = leased_connection(config.database_path)
    request = None
    try:
        request = runner(local_context(config, connection, paths))
    except Exception:
        _LOG.exception("%s failed", label)
        print(
            f"{label.capitalize()} failed. Any session it started is listed by:\n"
            "tmux -L remote-agents list-sessions",
            file=sys.stderr,
        )
        return 1
    finally:
        connection.close()
    return attach_to(request, switch_argv=switch_client_argv)


def _enter_pane(name: str, config_path: Path | None = None) -> int:
    """Compose and run one console pane surface — the same composition `tui` runs."""
    from remote_agents.adapters.tui.panes import run_pane_surface

    return _run_surface(
        config_path, lambda context: run_pane_surface(name, context), f"the {name} pane"
    )


def _private_state_config(config_path: Path, paths: ProductionPaths):
    """Load a configuration that may only write inside the private state directory."""
    config = load_config(config_path)
    if config.database_path != paths.database_path:
        raise ConfigError(
            "production database path must be "
            f"{paths.database_path}; refusing to write outside the private state directory"
        )
    return config


def _console_features_available(working_directory: Path) -> bool:
    """Probe, on a disposable socket, whether this host's tmux can host the console.

    `doctor` is the one command an operator runs before trusting a deploy, so the console's
    window contract is proved here — by the same round trip the console will actually make —
    rather than discovered as a mid-composition failure the first time the surface starts.
    Any failure is a plain no: the probe is diagnosis, never a gate that can crash `doctor`.
    """
    from remote_agents.adapters.tmux.feature_probe import probe_features

    try:
        return probe_features(working_directory).panes_splittable
    except Exception:  # noqa: BLE001 — a diagnostic probe reports, it never raises
        return False


def _command_succeeds(argv: tuple[str, ...]) -> bool:
    """Check one fixed local dependency command without a shell or captured content."""
    try:
        completed = subprocess.run(
            argv,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _doctor_report(paths: ProductionPaths, config, drift: dict[str, object]) -> dict[str, object]:
    """Build the installed service's health report -- the one `doctor` prints, for both callers.

    Extracted from `doctor`'s own branch when onboarding needed to end with it. Extracted rather
    than reimplemented, and that is the whole decision: a bespoke "did that work?" summary at the
    end of onboarding would be a second report to keep in step with the first, and the second one
    is the one nobody remembers to update when a component is added. Now there is one function,
    and `doctor` and `onboard` cannot disagree about what a healthy host is.
    """
    registry = load_registry(config.registry_path)
    discovered = discover_projects(config.dev_root)
    catalogue = ProjectCatalogueProvider(config.registry_path, config.dev_root).refresh()
    profiles = probe_profiles(
        closed_profiles(),
        resolve=lambda executable: _resolve_profile_executable(executable, paths.home),
    )
    supervisor = _supervisor_for_host()
    return production_doctor(
        core_ready=registry.error is None,
        database_ready=database_is_ready(config.database_path),
        tmux_ready=_command_succeeds(("tmux", "-L", "remote-agents", "-V")),
        tmux_console_ready=_console_features_available(paths.home),
        telegram_ready=_telegram_credentials_are_private(paths),
        service_ready=_command_succeeds(supervisor.liveness_command()),
        profiles=profiles,
        registered_projects=len(registry.projects),
        discovered_projects=len(discovered),
        catalogue_projects=len(catalogue.catalogue),
        # Carried on the healthy path too, so a green report says the config *was* compared
        # rather than leaving the operator to infer it from the absence of a complaint. Silence
        # and a passed check look identical otherwise.
        config_drift=drift,
        credential_file=_credential_file_state(paths),
        platform=_host_platform(),
        supervisor_kind=supervisor.kind,
        liveness_meaning=supervisor.liveness_meaning,
        release=_release_state(),
    )


def _telegram_credentials_are_private(paths: ProductionPaths) -> bool:
    """Verify only the private credential-file boundary; never read or print its values."""
    try:
        paths.require_private_environment()
    except ConfigError:
        return False
    return True


def _credential_file_state(paths: ProductionPaths) -> dict[str, object]:
    """Ask the in-process parser whether the credential file still resolves, without reading it out.

    This is the check that makes retiring `EnvironmentFile=` safe to do at all. While systemd
    read the file, its parser was the one that mattered and this one was exercised only on
    macOS; afterwards ours is the only reader on both platforms. The two disagree about quoted
    values, `;` comments, lines without `=`, backslash escapes and line continuations, so a
    file that started the service yesterday can refuse to start it after the unit changes --
    and the previous Telegram check would still report green, because it stats permissions
    without parsing.

    Nothing about the file's contents reaches the report: a diagnostic that prints the token to
    explain that the token is wrong has done more damage than the fault it names.
    """
    try:
        paths.require_private_environment()
    except ConfigError:
        # Already reported by the `telegram` component; named here so the two agree.
        return credential_file_report(
            readable=False, names_resolved=False, reason="credential_file_unavailable"
        )
    try:
        _load_private_telegram_secrets(paths)
    except ConfigError:
        return credential_file_report(
            readable=True, names_resolved=False, reason="credential_file_unresolved"
        )
    return credential_file_report(readable=True, names_resolved=True, reason=None)


def _resolve_serve_secrets(
    paths: ProductionPaths, *, environment: Mapping[str, str] | None = None
) -> TelegramSecrets:
    """Resolve the Telegram credential for a serving process, from either supported source.

    The environment is tried first and the checked private file second, and that order is the
    decision rather than an implementation detail.

    **The original reason has since expired, and the ordering is now kept for a different one
    -- recorded rather than quietly re-justified.** It was: on the Linux host the two sources
    were the same path, because the unit's `EnvironmentFile=` named exactly `environment_path`,
    so ordering could not change which values arrived, only which *parser* read them; env-first
    kept the running host on systemd's parser and was said to "stop mattering the day
    `EnvironmentFile=` leaves the unit". Task 2.0 was that day. No unit declares
    `EnvironmentFile=` any more, so systemd injects nothing, the environment is normally empty
    for a serving process, and on both platforms the file is what is actually read.

    What the ordering does now is narrower and worth keeping: it lets an operator override the
    file for one invocation without editing it -- exporting the three variables to reproduce a
    fault, or to run against a second bot -- and it keeps any host that still injects them
    (a hand-written unit, a shell wrapper, a container) working exactly as before rather than
    being silently switched to a different source by an upgrade. Both are reasons to prefer an
    explicit, per-process signal over a file on disk, which is the general form of the rule.

    The fallback is what makes a launchd host possible at all. `launchd.plist(5)` has no
    `EnvironmentFile` equivalent, and its only mechanism -- `EnvironmentVariables` -- puts the
    value inside the plist, where `launchctl print` reads it back. So on macOS nothing injects
    the variables and the file is the only source; `require_private_environment` has always
    enforced 0600, owner and regular-file-ness on it, by the same POSIX calls on both platforms.
    That used to end "no test runs on Darwin yet, so that is a claim about the code, not a
    measured one". It is measured now: the two-OS CI matrix runs `tests/integration` -- which
    holds `test_secret_sources.py` -- on `macos-latest` on every push, and the macOS acceptance
    drill onboarded a real Mac whose credential file `require_private_environment` accepted.

    **A partial environment refuses rather than falling back**, and that distinction is the
    reason this is a function rather than an `or`. Absent means nothing injected the variables,
    which is what a launchd host looks like. Partial means something tried and got it wrong --
    a typo'd variable name, a rotation that rewrote only the token line -- and the two are
    indistinguishable to a check that merely asks whether all three arrived. Falling back there
    would start the service on the *previous* credential and say nothing, which is strictly
    worse than the pre-existing behaviour it would replace: both serve call sites used to reach
    `load_secrets()` at its raising default, so any missing variable stopped the process.
    Nothing downstream would catch it either -- `doctor`'s Telegram component checks the file's
    permissions, not which credential the running service actually resolved.
    """
    values = os.environ if environment is None else environment
    # **Membership, not truthiness.** A blank assignment -- `REMOTE_AGENTS_OWNER_CHAT_ID=` --
    # is a line somebody wrote, and one upstream template variable going empty blanks all three
    # at once. Asking whether any *value* is truthy answers "no" for that file exactly as it
    # does for a host that injected nothing, so the resolver would fall back and serve the
    # previous credential without a word. Asking whether the *key* is there separates "nothing
    # ran" from "something ran and produced nothing".
    if any(name in values for name in TELEGRAM_SECRET_VARIABLES):
        # An injection mechanism is present and is expected to supply all three.
        # `production=True` is what turns a gap -- missing or blank, which `load_secrets`
        # already treats alike -- into a ConfigError naming the variables, rather than a silent
        # fall-through to a different credential.
        injected = load_secrets(values)
        assert injected is not None
        return injected
    return _load_private_telegram_secrets(paths)


def _load_private_telegram_secrets(paths: ProductionPaths) -> TelegramSecrets:
    """Read the checked private credential file, for the audit *and* for a serving process.

    It had one caller when it was written -- the read-only `telegram-ui-audit` -- and the
    docstring said so. It now has two: `_resolve_serve_secrets` reaches it on any host where
    nothing injected the variables, which is every launchd host. That makes this a live
    credential path rather than a diagnostic one, so an error path loosened or a result cached
    here on the assumption that only a diagnostic reads it would change what the running
    service authenticates as.
    """
    environment_path = paths.require_private_environment()
    environment: dict[str, str] = {}
    try:
        contents = environment_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        # The fourth member of the decode class swept in this stage, and the only one that
        # had no handler at all: every other malformed-environment-file path here raises
        # ConfigError, so a truncated or wrongly-encoded file was the one shape that came out
        # as a raw traceback. The message deliberately says nothing about the file's content
        # -- this is the credential file.
        raise ConfigError("Telegram environment file is unreadable") from error
    for line in contents.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        if not separator:
            raise ConfigError("Telegram environment file contains an invalid assignment")
        # A *matched* surrounding quote pair is stripped, because on the Linux host this file
        # and the unit's `EnvironmentFile=` are the same path -- so systemd's parser reads it
        # there and this one reads it on macOS, and the two disagreeing means identical bytes
        # produce two different bot tokens. systemd unquotes; a bare `partition` would keep the
        # quotes and authenticate as `"token"`, failing at runtime with nothing pointing back
        # here. Unbalanced quotes are left alone rather than half-eaten.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        environment[name] = value
    secrets = load_secrets(environment)
    assert secrets is not None
    return secrets


def _print_session_history(arguments) -> int:
    """Print one session's recorded lifecycle events (BL-030).

    Reads through the same private-state guard every other command uses, so a history read
    cannot be pointed at a database outside the owner's state directory. Nothing here is
    mutable and nothing is sent anywhere -- it is the read half the table has been missing.
    """
    paths = ProductionPaths.for_home(Path.home())
    try:
        # Called for its refusal, not its value: `_private_state_config` raises when the
        # config names a database outside the owner's private state directory, which is what
        # stops a history read being pointed at an arbitrary file.
        _private_state_config(arguments.config or paths.config_path, paths)
        session_id = SessionId.parse(arguments.history)
    except (ConfigError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    connection = paths.open_database(open_database, migrations=MIGRATIONS)
    try:
        store = SQLiteSessionStore(connection)
        record = asyncio.run(store.get(session_id))
        if record is None:
            print(f"no session recorded for {session_id}", file=sys.stderr)
            return 1
        events = asyncio.run(store.events(session_id))
    finally:
        connection.close()
    if arguments.json:
        print(
            json.dumps(
                {
                    "session": str(session_id),
                    "state": record.state.value,
                    "events": [
                        {
                            "event": event.event_type,
                            "at": event.created_at.isoformat(),
                            "error_code": event.error_code,
                        }
                        for event in events
                    ],
                },
                sort_keys=True,
            )
        )
        return 0
    print(f"{session_id} · {record.state.value}")
    for event in events:
        suffix = f" ({event.error_code})" if event.error_code else ""
        print(f"  {event.created_at.isoformat()}  {event.event_type}{suffix}")
    return 0
