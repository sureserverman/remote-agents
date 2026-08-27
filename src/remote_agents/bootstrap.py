"""Composition root for the private Telegram control-plane service."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from remote_agents import __version__
from remote_agents.adapters.agents.catalogue import ProfileConversationCatalogue
from remote_agents.adapters.agents.claude_sessions import ClaudeSessionCatalogue
from remote_agents.adapters.agents.codex_sessions import CodexAppServerClient, CodexSessionCatalogue
from remote_agents.adapters.agents.cursor_sessions import CursorSessionCatalogue
from remote_agents.adapters.agents.hook_install import (
    HookInstallError,
    default_settings_path,
    install_agent_hooks,
    remove_agent_hooks,
)
from remote_agents.adapters.agents.opencode_sessions import (
    OpenCodeCliRunner,
    OpenCodeSessionCatalogue,
)
from remote_agents.adapters.agents.usage import ProfileUsageReaders
from remote_agents.adapters.projects.discovery import discover_projects
from remote_agents.adapters.projects.registry import load_registry
from remote_agents.adapters.projects.registry_writer import RegistryProjectRecorder
from remote_agents.adapters.projects.workspace import FilesystemProjectWorkspace
from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.sqlite.callback_state_store import SQLiteCallbackStateStore
from remote_agents.adapters.sqlite.chat_view_store import SQLiteChatViewStore
from remote_agents.adapters.sqlite.database import (
    database_is_ready,
    leased_connection,
    open_database,
    restore_database,
)
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.standing_notification_store import (
    SQLiteStandingNotificationStore,
)
from remote_agents.adapters.supervisor.launchd import LaunchdSupervisor
from remote_agents.adapters.supervisor.systemd import SystemdSupervisor
from remote_agents.adapters.telegram.service import (
    PrivateBotBoundary,
    audit_owner_metadata,
    build_private_bot,
    run_private_bot,
)
from remote_agents.adapters.tmux.codec import attach_argv, switch_client_argv
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import (
    build_launch_profile,
    build_resume_profile,
    probe_profiles,
)
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.adapters.tui import PANE_NAMES
from remote_agents.application.console import RecoveryReport

if TYPE_CHECKING:
    # Annotations only. The terminal's own modules are imported inside the functions that
    # need them, so `serve` never loads the terminal library and a failure in it cannot
    # reach the bot — naming them here would undo that.
    from remote_agents.adapters.tui.context import TuiContext
    from remote_agents.adapters.tui.model import AttachRequest
from remote_agents.adapters.supervisor.installer import (
    DaemonInstallError,
    install_daemon,
    remove_daemon,
)
from remote_agents.agent_event import spool_from_stdin
from remote_agents.application.activity import PaneQuietWatcher, drain_activity
from remote_agents.application.backend import Backend
from remote_agents.application.conversations import ConversationService
from remote_agents.application.dependencies import (
    MISSING,
    PackageManager,
    confirm_and_install,
    probe_dependencies,
    render_remediation,
)
from remote_agents.application.doctor import (
    credential_file_report,
    production_doctor,
    profile_doctor,
)
from remote_agents.application.errors import ProjectCreationError
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_admin import CreateProjectCommand, ProjectCreationService
from remote_agents.application.project_catalog import CatalogProject, build_catalogue
from remote_agents.application.reconcile import ReconciliationService, SessionLocks
from remote_agents.application.releases import (
    is_release_tag,
    newest_release,
    release_status,
    upgrade_available,
)
from remote_agents.application.services import SessionService
from remote_agents.config import (
    TELEGRAM_SECRET_VARIABLES,
    ConfigError,
    TelegramSecrets,
    describe_schema_drift,
    load_config,
    load_secrets,
    render_config,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.profiles import ProfileCompatibility, closed_profiles
from remote_agents.ports.agent_activity import AgentActivity
from remote_agents.ports.agent_usage import AgentUsage, UsageQuery
from remote_agents.ports.argv_text import (
    NonEchoingArgumentParser,
    refuse_a_credential_shaped_value,
)
from remote_agents.ports.service_supervisor import ServiceSupervisor, SupervisorKind
from remote_agents.production import ProductionPaths

_LOG = logging.getLogger(__name__)
_RECONCILE_INTERVAL_SECONDS = 60.0
_ACTIVITY_POLL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ProjectCatalogueSnapshot:
    """One consistent view of the projects a surface may currently offer."""

    catalogue: tuple[CatalogProject, ...]
    registry_error: str | None


class ProjectCatalogueProvider:
    """Re-read the registry and discovery so a new project needs no service restart.

    The path mapping is mutated in place rather than replaced, so every consumer holding
    it — the terminal and the provider conversation catalogues — observes one live view.
    """

    def __init__(self, registry_path: Path, dev_root: Path) -> None:
        self._registry_path = registry_path
        self._dev_root = dev_root
        self._paths: dict[ProjectId, Path] = {}
        self._snapshot = ProjectCatalogueSnapshot((), None)

    @property
    def paths(self) -> Mapping[ProjectId, Path]:
        """Return a live read-only view; only refresh may change the shared routing table."""
        return MappingProxyType(self._paths)

    @property
    def snapshot(self) -> ProjectCatalogueSnapshot:
        return self._snapshot

    def refresh(self) -> ProjectCatalogueSnapshot:
        """Rebuild the catalogue and path mapping from the current registry and filesystem."""
        registry = load_registry(self._registry_path)
        discovered = discover_projects(self._dev_root)
        resolved: dict[ProjectId, Path] = {}
        offerable = []
        for project in (*registry.projects, *discovered):
            canonical = _resolved_project_path(project.path)
            if canonical is None:
                _LOG.warning("skipping catalogued project whose directory is unreachable")
                continue
            resolved[ProjectId(_opaque_id(canonical))] = canonical
            offerable.append(project)
        registered = [project for project in registry.projects if project in offerable]
        found = [project for project in discovered if project in offerable]
        catalogue = build_catalogue(registered, found, registry_error=registry.error)
        if registry.error is not None:
            _LOG.warning("project registry is degraded: %s", registry.error)
        self._publish(resolved)
        self._snapshot = ProjectCatalogueSnapshot(catalogue, registry.error)
        return self._snapshot

    def _publish(self, resolved: dict[ProjectId, Path]) -> None:
        """Apply the new mapping without ever hiding a project that survives the refresh.

        Consumers read the shared mapping without holding a lock, so clearing it first would
        expose a window where a valid launch resolves to nothing. Adding before removing means
        a surviving project is never absent; at worst a removed one lingers for an instant.
        """
        self._paths.update(resolved)
        for stale in [key for key in self._paths if key not in resolved]:
            del self._paths[stale]


@dataclass(frozen=True, slots=True)
class ServiceComposition:
    """The bot boundary plus what the service needs to keep records honest beside it."""

    boundary: PrivateBotBoundary
    terminal: TmuxTerminal
    reconciler: ReconciliationService
    quiet_watcher: PaneQuietWatcher | None = None
    """None only in compositions that do not wire pane watching, which today means tests.

    Production always supplies one -- `_private_boundary` builds it unconditionally -- and it
    simply has nothing to do on a pass where no hookless-profile session is running. The field
    is optional so that every composition predating it still constructs.
    """

    activity_directory: Path | None = None
    """Where the agent hooks spool what they reported, or None when nothing spools.

    The second of the two activity sources, and the reason the periodic pass runs even for a
    composition with no quiet watcher: a host running only Claude sessions has nothing to watch
    a pane for and everything to deliver.
    """

    activity_store: SQLiteActivityStore | None = None
    """Where every observation becomes durable before delivery, or None to skip recording.

    The local feed's source (migration 9), never a delivery ledger — DEC-026's in-memory
    notifier state is unchanged. Recorded *before* `deliver` so the feed shows what was
    observed even when Telegram refuses the send; a failed append costs the feed one row
    and never costs the phone its notification.
    """


async def _serve_with_reconciliation(
    secrets: TelegramSecrets,
    composition: ServiceComposition,
    serve_runner: Callable[[TelegramSecrets, PrivateBotBoundary], Awaitable[None]],
    interval: float,
    activity_interval: float = _ACTIVITY_POLL_SECONDS,
) -> None:
    """Poll Telegram while keeping durable records agreeing with observed panes.

    A launch that raises after its record is saved leaves that record STARTING, which no
    owner action can resolve, so reconciliation runs once before polling and periodically
    beside it. It never interrupts the service: a reconciliation that fails is logged and
    the pass is skipped, because a service that stops polling is worse than one whose
    records are briefly stale.

    RuntimeCoordinator composes the same three parts, but it treats polling returning as a
    failure; run_private_bot returns normally on SIGTERM, so adopting it would mean moving
    signal handling out of the polling boundary. That is a larger change to the shutdown
    path than this repair warrants.
    """
    # Rank before the first screen can be drawn. The composition hands the catalogue over in
    # registry order and the ranking is applied on refresh, so without this every start and
    # restart served an unranked Launch, Resume and search until the owner happened to press
    # Refresh — the common case, and the first thing an acceptance run looks at. It lives here
    # rather than inside the long-poll runner because `main` lets a test substitute the runner,
    # which makes this line reachable by a test; the runner is not.
    await composition.boundary.refresh_catalogue()
    await _reconcile_quietly(composition)
    periodic = [asyncio.create_task(_reconcile_periodically(composition, interval))]
    if composition.quiet_watcher is not None or composition.activity_directory is not None:
        # A separate task rather than another step inside the reconciliation pass: the two
        # answer different questions on different clocks, and a pane capture that hangs must
        # not stop records being reconciled. Nothing is polled before the service is serving,
        # unlike reconciliation -- a first pass at start-up could only establish the baseline
        # the classifier already refuses to report on.
        #
        # Either source is reason enough to run it. A host serving only Claude sessions has no
        # pane to watch and a spool full of what those sessions reported, and gating the whole
        # pass on the watcher would have delivered none of it.
        periodic.append(
            asyncio.create_task(_watch_quiet_periodically(composition, activity_interval))
        )
    try:
        await serve_runner(secrets, composition.boundary)
    finally:
        for task in periodic:
            task.cancel()
        await asyncio.gather(*periodic, return_exceptions=True)


async def _watch_quiet_periodically(composition: ServiceComposition, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        await _watch_quiet_once(composition)


async def _watch_quiet_once(composition: ServiceComposition) -> None:
    """One pass over both activity sources, delivered — and never raising.

    This loop runs beside the one that serves the owner, so a failure anywhere in it is logged
    and costs one pass. The two sources are gathered into one list on purpose: they answer the
    same question about different profiles, and an observation is owed the same weight
    regardless of which of them noticed.

    Gathering them is also what lets the notifier group by session across both, which it could
    not do if each source delivered its own batch. In practice the two never meet in one group
    -- `HOOK_SOURCED_PROFILES` is subtracted from the pane watch precisely so a session is
    watched or hooked and never both -- so this is a property of the seam rather than a case
    anyone will see. It is worth stating because the alternative shape, one `deliver` call per
    source, looks equivalent and quietly reintroduces two messages per session per pass.

    **Each source is guarded separately, and that is not tidiness.** `poll()` commits its own
    dedup state as a side effect of deciding a pane has gone quiet -- it marks the spell
    reported before the activity reaches anyone, and re-arms only when the pane changes again.
    Under one shared `try`, a drain that raised after a successful poll discarded that already
    committed observation, and the quiet spell was then never reportable at all. The failure is
    invisible: nothing is lost that anything counts, and the owner simply never hears about an
    agent that stopped.

    `deliver` is called even when both sources yielded nothing, because it also drains the
    retry queue an earlier pass may have left behind; returning early on an empty list would
    strand a backlog for as long as nothing new happened.

    The drain is a synchronous directory walk that unlinks what it reads, so it goes to a
    thread: this coroutine shares its event loop with Telegram long-polling and pane captures,
    and a spool with a backlog would otherwise stall both.
    """
    activities: list[AgentActivity] = []
    if composition.quiet_watcher is not None:
        try:
            activities.extend(await composition.quiet_watcher.poll())
        except Exception:
            _LOG.exception("pane quiet watch failed")
    if composition.activity_directory is not None:
        try:
            activities.extend(
                await asyncio.to_thread(drain_activity, composition.activity_directory)
            )
        except Exception:
            _LOG.exception("draining the activity spool failed")
    if composition.activity_store is not None:
        for activity in activities:
            try:
                await composition.activity_store.append(activity)
            except Exception:
                _LOG.exception("recording an activity observation failed; delivery continues")
    try:
        await composition.boundary.notifier.deliver(activities)
    except Exception:
        _LOG.exception("delivering activity notifications failed")


async def _reconcile_periodically(composition: ServiceComposition, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        await _reconcile_quietly(composition)


async def _reconcile_quietly(composition: ServiceComposition) -> None:
    try:
        await composition.reconciler.reconcile(await composition.terminal.managed_observations())
    except Exception:
        _LOG.exception("reconciliation pass failed")


def _resolved_project_path(path: Path) -> Path | None:
    """Skip a catalogued directory that has since been moved or removed."""
    try:
        return path.resolve(strict=True)
    except OSError:
        return None


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
    install_hooks_parser.add_argument("--settings", type=Path)
    install_hooks_parser.add_argument("--activity-dir", type=Path)
    install_hooks_parser.add_argument("--remove", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.command == "agent-event":
        # Delegated rather than implemented here: `__main__` routes the installed hook command
        # straight to that module without importing this one, and two copies of a path that
        # promises never to raise would eventually stop agreeing about how it does that.
        return spool_from_stdin(arguments.activity_dir)
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
            settings_path = arguments.settings or default_settings_path(Path.home())
            outcome = (
                remove_agent_hooks(settings_path)
                if arguments.remove
                else install_agent_hooks(settings_path, activity_directory=arguments.activity_dir)
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


@dataclass(frozen=True, slots=True)
class LocalRuntime:
    """The terminal and profile availability every local surface composes identically."""

    terminal: TmuxTerminal
    # What the probe observed, before anything narrowed it. This used to sit beside a
    # `profiles` field holding the Telegram wizard's narrowing, which the local surface then
    # converted back -- two narrowings of one probe, free to diverge. `compose_backend` now
    # narrows this once into `Backend.profiles` and both surfaces read that.
    compatibility: tuple[ProfileCompatibility, ...]
    # The gateway the terminal wraps, carried separately so the composition root can wire
    # console capabilities (client switching) without widening the terminal port for a
    # concern that is presentation, not lifecycle.
    gateway: TmuxGateway


#: The variables a managed pane inherits from whatever composed the runtime.
#:
#: Deliberately tiny: the agent is launched through `os.execvpe`, which *replaces* the
#: environment rather than adding to it, so this tuple is the whole world the process gets.
_INHERITED_ENVIRONMENT = ("HOME", "LANG", "LC_ALL", "PATH", "TERM", "COLORTERM")

#: What `TERM` becomes when the composing process has none, and the values that count as none.
#:
#: `execvpe` replacing the environment is also why tmux's own `default-terminal` never reaches
#: the agent: tmux sets `TERM` for the shell it spawns, the fixed runner then execs over it
#: with exactly the mapping above, and whatever tmux set is gone. So the value here is the
#: only `TERM` an agent ever sees.
#:
#: The bot is a **systemd user service**, and a systemd service has no controlling terminal
#: and therefore no `TERM`. The local surface is a TUI and always has one. That single
#: difference is why a session launched from the bot rendered in white while the identical
#: session launched from the TUI rendered in colour: with `TERM` absent, every agent CLI's
#: colour detection (`supports-color`, and the equivalents in the Rust and Go CLIs) reports no
#: capability and falls back to monochrome. Verified against the stored launch intents on this
#: host — bot-launched intents carried no `TERM` key at all, TUI-launched ones carried
#: `xterm-256color` — rather than inferred from the symptom.
#:
#: `xterm-256color` because it is the entry the TUI-launched panes were already proving works,
#: and because it is present in the base terminfo database of every platform this project
#: supports. `dumb` is treated as absent for the same reason it exists: it is the value a
#: process announces when it knows nothing about its terminal, and a pane on this socket
#: always has one.
_DEFAULT_TERM = "xterm-256color"
_COLOURLESS_TERMS = frozenset({"", "dumb", "unknown"})


def _curated_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Return the launch environment every managed pane gets, with a usable `TERM` guaranteed.

    `COLORTERM` is inherited but never invented: it is the terminal's own claim about truecolour
    support, and a composing process that has one is passing on something it was told. Asserting
    it on behalf of a service that has no terminal would be this function guessing at a
    capability instead of supplying a missing default.
    """
    environment = {name: source[name] for name in _INHERITED_ENVIRONMENT if name in source}
    if environment.get("TERM", "").strip().lower() in _COLOURLESS_TERMS:
        environment["TERM"] = _DEFAULT_TERM
    return environment


def _local_runtime(config, paths: ProductionPaths, project_paths) -> LocalRuntime:
    """Compose the one tmux terminal and profile probe that every surface shares."""
    definitions = closed_profiles()
    compatibility = probe_profiles(
        definitions,
        resolve=lambda executable: _resolve_profile_executable(executable, paths.home),
    )
    definitions_by_id = {definition.profile_id: definition for definition in definitions}
    executables = {
        result.profile_id: _resolve_profile_executable(
            definitions_by_id[result.profile_id].executable, paths.home
        )
        for result in compatibility
    }
    profile_factories = {}
    resume_profile_factories = {}
    allowed_environment = _curated_environment(os.environ)
    profile_directories = sorted(
        {str(executable.parent) for executable in executables.values() if executable is not None}
    )
    allowed_environment["PATH"] = ":".join(
        (*profile_directories, allowed_environment.get("PATH", ""))
    ).rstrip(":")
    for result in compatibility:
        executable = executables[result.profile_id]
        if result.available and executable is not None:
            definition = definitions_by_id[result.profile_id]
            profile_factories[result.profile_id] = _profile_factory(
                definition, executable, allowed_environment
            )
            resume_profile_factories[result.profile_id] = _resume_profile_factory(
                definition, executable, allowed_environment
            )
    gateway = TmuxGateway(
        "remote-agents", AsyncTmuxRunner(), intent_directory=paths.intent_directory
    )
    terminal = TmuxTerminal(
        gateway,
        project_paths,
        {},
        startup_timeout=20,
        profile_factories=profile_factories,
        resume_profile_factories=resume_profile_factories,
    )
    return LocalRuntime(terminal, compatibility, gateway)


def _narrow_profiles(
    compatibility: tuple[ProfileCompatibility, ...],
) -> tuple[ProfileAvailability, ...]:
    """Narrow the probe's record into the one type both surfaces read.

    `ProfileCompatibility.reason` carries two different facts in one field, and which one it
    is holding is decided by `available`: on a blocked profile it says why it is blocked, on
    an available one it says why no version is being shown. `probe_profiles` produces exactly
    those two plus the quiet case, so the split is total and this is the only place it needs
    to be made.

    `status` and `version` are deliberately not carried through. Neither surface renders
    them: the bot shows a label and one reason string, the local surface shows a label and a
    reason only where it refuses. The reader that does want them is `doctor`, and it does not
    take them from here -- `doctor --profiles` runs its own `probe_profiles` and hands
    the domain tuple straight to `profile_doctor`. Narrowing them away costs that reader
    nothing (DEC-002 -- a version is diagnosis, not a gate).
    """
    return tuple(
        ProfileAvailability(
            str(profile.profile_id),
            profile.available,
            blocked_reason=None if profile.available else profile.reason,
            note=profile.reason if profile.available else None,
        )
        for profile in compatibility
    )


def _usage_reader(
    store: SQLiteSessionStore, project_paths: Mapping[ProjectId, Path]
) -> Callable[[SessionId], Awaitable[AgentUsage | None]]:
    """Bind the provider usage readers to the two things only the root knows.

    A session is a row; a provider conversation is a file in a directory named after a
    workspace. Turning the first into the second needs the store *and* the project paths, and
    neither belongs on a screen builder — which is why `Backend.usage` is a bound callable in
    the shape of `capture` rather than a service the frontends resolve themselves.

    The provider read runs on a worker thread. It is a `stat` sweep of a directory and a tail
    read of one file — small, but a filesystem walk all the same, and the same rule
    `refresh_catalogue` states applies here: neither frontend may block its event loop on the
    disk during a render. The store lookup ahead of it is already `async` and stays on the
    loop, because that is how every other caller drives it and it is a single indexed row.
    """
    readers = ProfileUsageReaders()

    async def read(session_id: SessionId) -> AgentUsage | None:
        record = await store.get(session_id)
        if record is None:
            return None
        workspace = project_paths.get(record.project_id)
        if workspace is None:
            return None
        query = UsageQuery(record.profile_id, workspace, record.created_at, record.resume_source_id)
        return await asyncio.to_thread(readers.read, query)

    return read


def compose_backend(
    config,
    connection,
    paths: ProductionPaths,
    *,
    projects: ProjectCatalogueProvider | None = None,
    runtime: LocalRuntime | None = None,
    store: SQLiteSessionStore | None = None,
    locks: SessionLocks | None = None,
    hide_in_console: Callable[[SessionId], Awaitable[None]] | None = None,
    activity_feed: Callable[[], Awaitable[tuple[AgentActivity, ...]]] | None = None,
) -> Backend:
    """Build the one backend a process hands to its frontend (ARCH-B1, ARCH-B2).

    Both compositions below are built from this. What used to be four call sites that
    happened to agree — `ProjectCatalogueProvider`, `_local_runtime`, `_conversation_service`,
    `_project_creator` — is now one function, so a capability added to one surface cannot
    silently miss the other.

    **The connection is the caller's, and this must never open one.** `serve` holds a single
    connection for the life of the process; a surface holds one only for the duration of a
    single store operation, which is the guarantee DEC-035 put in place of the old exec-away
    contract and the README states in those words. DEC-005's five concurrent writers are
    sound only because of that lease, so a backend that opened its own handle would not be a
    simplification — it would remove the thing making the writer count safe.

    **`projects` and `runtime` are parameters, not internals**, because the caller needs them
    anyway for the wiring this function deliberately does not do: the service needs the
    terminal and the gateway for its reconciler, quiet watcher and console composer, and the
    surface needs the gateway for console hosting. Passing them in is what stops the profile
    probe — which shells out once per profile — from running twice in one process. Omitted,
    they are built here, which is what a test composing a bare backend wants.

    Passing `projects` in does **not** save a catalogue refresh — this always calls
    `refresh()`, deliberately, so the backend's snapshot is its own rather than whatever the
    caller last read. That is a filesystem walk, not a probe, and the asymmetry with
    `runtime` is intentional: do not "fix" the apparent double refresh by trusting the
    caller's snapshot.

    **`store` is a parameter for the same reason**: the service composition already builds
    one for its reconciler and quiet watcher, and all three consumers are meant to be looking
    at the same store.

    **`activity_feed` is a parameter for a narrower reason:** the reader is bounded by
    `FEED_LIMIT`, which lives in the terminal package, and importing it here would make the
    service load the terminal library at composition time — the exact property
    `local_context`'s docstring promises it does not.
    """
    projects = projects or ProjectCatalogueProvider(config.registry_path, config.dev_root)
    catalogue = projects.refresh().catalogue
    runtime = runtime or _local_runtime(config, paths, projects.paths)
    return Backend(
        sessions=SessionService(
            store if store is not None else SQLiteSessionStore(connection),
            runtime.terminal,
            locks=locks,
            hide_in_console=hide_in_console,
        ),
        projects=_project_creator(config),
        conversations=_conversation_service(projects.paths),
        catalogue=catalogue,
        refresh_catalogue=lambda: projects.refresh().catalogue,
        # The one narrowing, for both surfaces. `ProfileCompatibility.reason` answers two
        # questions in one field -- why a profile is blocked, and why no version is shown --
        # so it is split here rather than at each surface, which is what let the two drift
        # and what took the local surface down on a probe that merely timed out.
        profiles=_narrow_profiles(runtime.compatibility),
        capture=runtime.terminal.capture,
        activity_feed=activity_feed,
        usage=_usage_reader(
            store if store is not None else SQLiteSessionStore(connection), projects.paths
        ),
        max_label_length=config.max_label_length,
    )


def _private_boundary(
    config, connection, paths: ProductionPaths, secrets: TelegramSecrets
) -> ServiceComposition:
    projects = ProjectCatalogueProvider(config.registry_path, config.dev_root)
    runtime = _local_runtime(config, paths, projects.paths)
    terminal = runtime.terminal
    store = SQLiteSessionStore(connection)
    # One lock map, shared by the two objects that write session state. See the note on the
    # ReconciliationService below: this single binding is the fix, and two instances here
    # would look identical and repair nothing.
    locks = SessionLocks()
    # **The bot arranges the console too, for one operation only: stepping it aside before a
    # stop destroys a pane.** Without this the owner stopping a displayed session from their
    # phone left the agent's pane to be killed *inside* the console window, so the console sat
    # a pane short — sessions and feed stretched across the whole width — until its next
    # reload put the projects surface back, up to ten seconds later.
    #
    # This is the half of DEC-005 that is answered rather than accepted. Its premise was one
    # writer over the panes by construction, and what made a second one safe is
    # `console_lock`: both composers are built by `_console_composer`, so both name the same
    # lock file, and neither decides from a reading the other is about to invalidate. The bot
    # never *builds* a console — nothing here calls `ensure` — and `hide` degrades to nothing
    # on a host with no console at all, which is every host that has not run `remote-agents`.
    console = _console_composer(runtime.gateway, paths.home)
    # The one backend this process hands its frontend (ARCH-B1). `locks` and the console
    # hide are the service's own wiring and go in here; the reconciler and quiet watcher
    # below are not the frontend's to drive and stay outside it (ARCH-B3).
    backend = compose_backend(
        config,
        connection,
        paths,
        projects=projects,
        runtime=runtime,
        # The same store the reconciler and quiet watcher below are given. Inert today --
        # SQLiteSessionStore holds only its connection -- but two instances where there was
        # one stops being inert the moment it gains a cache or a statement pool, and this
        # composition is the one place all three consumers are meant to agree.
        store=store,
        locks=locks,
        hide_in_console=console.hide,
    )
    return ServiceComposition(
        # The factory, not the class: it wires the stop controller, the live view and the
        # notifier, which the boundary used to build for itself out of whatever it had.
        build_private_bot(
            secrets.owner_user_id,
            secrets.owner_chat_id,
            # The durable store, not the in-memory default: a restart used to void every
            # button in the chat, and only this half of the pair actually fixes that.
            callbacks=SQLiteCallbackStateStore(connection),
            # And the durable anchor for the same reason: a restart that forgot which
            # message the live view is would send a second one and leave the first above it,
            # still holding buttons that — since Stage 1 — still resolve.
            anchors=SQLiteChatViewStore(connection),
            # And the durable standing notifications, which close the other half of that
            # same defect. A restart that forgot which message a session's notification is
            # sent a *second* one on the session's next report and left the first above the
            # live view — observed in the chat on 2026-08-20, when the 21:23 restart turned
            # one session's alert into one message above the menu and one below.
            standing=SQLiteStandingNotificationStore(connection),
            # The whole backend, not five of its fields taken out and handed over one at a
            # time. `catalogue` and `max_label_length` came through here too and are on it;
            # the boundary seeds its render copy of the first from `Backend.catalogue`.
            backend=backend,
            # Profiles come off the backend like everything else now. They were a separate
            # argument for as long as `Backend.profiles` held the domain type and this
            # surface needed its own narrowing; `compose_backend` does that narrowing once,
            # so the line that used to be the plausible-looking mistake is the correct one.
            profiles=backend.profiles,
            project_page_size=config.project_page_size,
        ),
        terminal,
        # Readiness is wired in deliberately: without it, reconciliation promotes any
        # FAILED session with a live pane to RUNNING, including one stopped dead on a
        # trust dialog it cannot answer. Observed in the wild 2026-08-14.
        #
        # The locks are shared with the SessionService above, and that sharing is the whole
        # fix for the InvalidTransition crashes: the reconciler runs on a timer beside the
        # service and writes `record_event` directly, so without a lock in common it would
        # overwrite the state of a session whose graceful stop is between its own two writes.
        # Constructing two SessionLocks here would type-check, run, and fix nothing.
        ReconciliationService(store, confirm_ready=terminal.confirm_ready, locks=locks),
        PaneQuietWatcher(store, terminal.capture, quiet_polls=config.activity_quiet_polls),
        paths.activity_directory,
        SQLiteActivityStore(connection),
    )


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


#: What the console's projects key runs — this program, asking for the surface back.
def _projects_command() -> tuple[str, ...]:
    """The argv the projects binding runs, built from this interpreter rather than a name.

    `sys.executable -m remote_agents` and not the bare `remote-agents` script, for the reason
    `create_console` already builds its dashboard command that way: the console is started
    from whatever interpreter the owner installed this into, and a root binding that assumed
    a console script on `PATH` would work on the developer's host and fail on a pipx install.
    """
    return (sys.executable, "-m", "remote_agents", "console", "projects")


def _console_composer(gateway=None, home: Path | None = None):
    """Build the one console composer shape, so four call sites cannot drift apart.

    They already had: `_enter_console`, `_console_arrange` and `local_context` each construct
    one, and only the last has a gateway of its own to reuse. What must not differ between
    them is the dashboard command, the projects command and the home directory — a composer
    that disagreed with its siblings about any of those would install a binding running a
    different program, or create a console somewhere else.

    **`_private_boundary` is the fourth, and it is why the lock file is supplied here rather
    than by each caller.** The bot arranges the console too now — it steps it aside before a
    stop destroys a pane — so the composers in two different processes have to be naming the
    same file or the lock excludes nothing. One factory, one path, and a caller cannot forget
    it. Derived from the owner's home the way every other production path is.
    """
    from remote_agents.application.console import ConsoleComposer
    from remote_agents.ports.console import ConsolePaneSlot

    return ConsoleComposer(
        gateway if gateway is not None else TmuxGateway("remote-agents", AsyncTmuxRunner()),
        (sys.executable, "-m", "remote_agents", "tui"),
        home if home is not None else Path.home(),
        projects_command=_projects_command(),
        arrangement_lock=ProductionPaths.for_home(
            home if home is not None else Path.home()
        ).console_lock_path,
        # One process per pane. Which entry point each pane runs is composition policy, the
        # same as which entry point *is* the dashboard, so it is decided here rather than
        # spelled inside the composer that arranges them.
        pane_commands={
            slot: (sys.executable, "-m", "remote_agents", "pane", name)
            for slot, name in (
                (ConsolePaneSlot.PROJECTS, "projects"),
                (ConsolePaneSlot.SESSIONS, "sessions"),
                (ConsolePaneSlot.FEED, "feed"),
            )
        },
    )


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


def _console_notes(composer, resident_pane: str | None) -> RecoveryReport | None:
    """Run the console's start-only repair and carry its report to the surface, or nothing.

    A named seam for two reasons. First, what it replaced was a `print` to stderr, and a
    `print` here is erased microseconds later when Textual takes the alternate screen —
    invisible for the entire session it describes; naming the hand-over lets a test assert
    that nothing reaches either stream, which is the actual defect.

    Second, **it must not be able to take the surface down**, and that is not free.
    `settle`'s own try block starts *after* it reads the pane arrangement, so a tmux hiccup
    there escapes it — and uncaught, it would reach `_run_surface`'s handler and exit instead
    of starting a degraded surface. DEC-040 restates the rule this protects: every composer
    method degrades to a log line, and a console that cannot be settled is still a console.
    Found by a Tier-2 review, which also noted the plan had promised this guarantee and never
    built it.
    """
    try:
        return asyncio.run(composer.settle(resident_pane))
    except Exception:
        _LOG.exception("the console could not be settled; the surface starts anyway")
        return None


def _console_opener(composer) -> Callable[[str], Awaitable[str | None]]:
    """What "open this session" means under console hosting: an exchange of panes.

    A named seam rather than a closure inside `local_context`, so the wiring can be asserted
    against the executed capability instead of against bootstrap's source text — a substring
    check for the same wiring once matched the *service* composition too, and deleting it
    from the local one left the suite green (`tests/integration/test_tui_bootstrap.py`).

    `show` and not `open`: DEC-039's accepted cost 1 names this replacement by hand. A tmux
    client attaches to a *session*, so the switch route lands wherever the vacated window
    ends up rather than on the agent; under the swap model the console reaches an agent by
    exchanging its left pane, which follows the pane whatever is hosting it (DEC-040).
    """

    async def open_in_console(session_id: str) -> str | None:
        return await composer.show(SessionId.parse(session_id))

    return open_in_console


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


def _conversation_service(project_paths) -> ConversationService:
    """The one conversation composition both surfaces use.

    Kept in a single function so the terminal cannot drift onto a different catalogue set
    than the service, which would let a conversation be resumable from one surface only.
    """
    return ConversationService(
        ProfileConversationCatalogue(
            {
                ProfileId("claude"): ClaudeSessionCatalogue(project_paths),
                ProfileId("codex"): CodexSessionCatalogue(project_paths, CodexAppServerClient()),
                ProfileId("opencode"): OpenCodeSessionCatalogue(project_paths, OpenCodeCliRunner()),
                ProfileId("cursor-agent"): CursorSessionCatalogue(),
            }
        )
    )


def local_context(config, connection, paths: ProductionPaths):
    """Compose the local terminal surface over the same store the service uses.

    The terminal's own modules are imported here rather than at module scope, so the
    service never loads the terminal library and a failure in it cannot reach serve.
    """
    from remote_agents.adapters.tui.attach import HostingMode, hosting_mode
    from remote_agents.adapters.tui.context import FEED_LIMIT, TuiContext

    projects = ProjectCatalogueProvider(config.registry_path, config.dev_root)
    runtime = _local_runtime(config, paths, projects.paths)

    open_in_console = None
    console_sync = None
    console_flash = None
    console_show_projects = None
    hide_in_console = None
    console_recovery = None
    if hosting_mode(os.environ) is HostingMode.CONSOLE:
        # Hosted by a client on our own server: opening a session **exchanges** its pane into
        # the console's left slot, every sessions reload notices what the other writer did to
        # whatever is displayed, and the surface stays alive. Everywhere else these fields
        # stay None and the surface keeps the exec-attach contract untouched. ensure() runs
        # before the app starts so the common failure is met here first and logged; the
        # capabilities are then wired regardless — deliberately, because under console hosting
        # an exec-attach would cost the surface its own process (attach.py), so a degraded
        # console keeps retrying quietly per pass rather than re-routing opens through exec.
        composer = _console_composer(runtime.gateway, paths.home)
        if not asyncio.run(composer.ensure()):
            # Wiring continues regardless (see above), but the operator hears about it
            # here once, at the surface's front door, not only in per-pass debug logs.
            console_recovery = RecoveryReport(
                (),
                (
                    "the console could not be prepared — check tmux on this host, "
                    "or run: remote-agents doctor",
                ),
                settled=False,
            )
        else:
            # The start-only repair, run by the process that *is* the console's window and by
            # nothing else — `_enter_console`'s throwaway composer must not, because entering
            # an already-running console is a re-entry rather than a start. What it could not
            # put right is told to the owner here, at the same front door: an unsettled
            # console reported only to a log is not reported.
            # `$TMUX_PANE` is this process's own pane. Passed so `settle` can refuse when the
            # dashboard is running somewhere other than the console's left slot: hosting is
            # decided by the socket name, which is true of every pane on this server.
            console_recovery = _console_notes(composer, os.environ.get("TMUX_PANE"))

        open_in_console = _console_opener(composer)
        console_sync = composer.sync
        console_flash = composer.flash
        console_show_projects = composer.show_projects
        # The stop paths ask the console to step out of the way before a pane is destroyed.
        # Wired only where a composer exists: elsewhere `SessionService` keeps the destruction
        # contract it has always had. The bot builds a composer of its own for this one
        # operation (see `_private_boundary`), so both writers now hide before destroying;
        # what still reaches `sync` is a hide that timed out, a degraded console, or a pane
        # that ended without either writer asking.
        hide_in_console = composer.hide

    # The same backend the service composes, over this process's leased connection
    # (ARCH-B1, ARCH-B2). The console capabilities above are this surface's alone and stay
    # out of it (ARCH-B3); `hide_in_console` is not one of those -- the bot wires its own,
    # from a hide-only composer -- so it goes in as a parameter here.
    backend = compose_backend(
        config,
        connection,
        paths,
        projects=projects,
        runtime=runtime,
        hide_in_console=hide_in_console,
        activity_feed=lambda: SQLiteActivityStore(connection).recent(limit=FEED_LIMIT),
    )
    return TuiContext(
        # The whole backend, as `_private_boundary` hands the bot the same object. What used
        # to be eight arguments taken out of it one at a time -- launcher, creator,
        # refresh_catalogue, catalogue, capture, conversations, activity_feed,
        # max_label_length -- is one, so a capability added to the backend cannot reach one
        # surface and miss the other.
        backend=backend,
        # The same tuple the bot gets, from the same narrowing (`_narrow_profiles`). This
        # was a second narrowing, and its comment recorded why it had to drop the reason:
        # `ProfileCompatibility.reason` meant either "blocked because" or "no version
        # because", and this surface's old type read any reason as blocking, so passing it
        # through unconditionally took the whole surface down with `an available profile has
        # no blocking reason` when a version probe merely timed out. Dropping the note
        # avoided the crash and lost the diagnostic. Splitting the field means the note now
        # *reaches* this surface -- `launch.py` still renders only `blocked_reason`, so an
        # owner here sees no difference yet between a quiet probe and one that timed out.
        # DEC-045 accepted cost 1. What changed is that the information is present to render,
        # rather than discarded three layers earlier.
        profiles=backend.profiles,
        # Per-surface, and staying that way: DEC-039 keeps the attach route this surface's
        # own rather than following the host the way the bot's does.
        attach_argv=lambda session_id: attach_argv(SessionId.parse(session_id)),
        open_in_console=open_in_console,
        console_sync=console_sync,
        console_flash=console_flash,
        console_show_projects=console_show_projects,
        console_recovery=console_recovery,
        # The declared boundary's answer to where a surface preference lives, not this
        # surface's own (DEC-046): the path is wired here and read through a total reader.
        preferences_path=paths.preferences_path,
    )


def _project_creator(config) -> ProjectCreationService:
    """Compose the one project-creation service every local surface shares."""
    return ProjectCreationService(
        FilesystemProjectWorkspace(config.dev_root),
        RegistryProjectRecorder(config.registry_path, config.dev_root),
    )


def _opaque_id(path: Path) -> str:
    return sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()[:24]


def _profile_factory(definition, executable: Path, environment: dict[str, str]):
    return lambda session_id: build_launch_profile(definition, executable, session_id, environment)


def _resume_profile_factory(definition, executable: Path, environment: dict[str, str]):
    return lambda session_id, source_id: build_resume_profile(
        definition, executable, session_id, source_id, environment
    )


def _resolve_profile_executable(executable: str, home: Path) -> Path | None:
    for candidate in (
        home / ".local" / "bin" / executable,
        *sorted((home / ".nvm" / "versions" / "node").glob(f"*/bin/{executable}")),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    resolved = shutil.which(executable)
    return Path(resolved) if resolved is not None else None


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
