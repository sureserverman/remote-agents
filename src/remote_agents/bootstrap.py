"""Composition root for the private Telegram control-plane service."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

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
from remote_agents.adapters.telegram.service import (
    PrivateBotBoundary,
    audit_owner_metadata,
    run_private_bot,
)
from remote_agents.adapters.telegram.wizard import ProfileAvailability
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
from remote_agents.agent_event import spool_from_stdin
from remote_agents.application.activity import PaneQuietWatcher, drain_activity
from remote_agents.application.backend import Backend
from remote_agents.application.conversations import ConversationService
from remote_agents.application.doctor import production_doctor, profile_doctor
from remote_agents.application.errors import ProjectCreationError
from remote_agents.application.project_admin import CreateProjectCommand, ProjectCreationService
from remote_agents.application.project_catalog import CatalogProject, build_catalogue
from remote_agents.application.reconcile import ReconciliationService, SessionLocks
from remote_agents.application.services import SessionService
from remote_agents.config import (
    ConfigError,
    TelegramSecrets,
    describe_schema_drift,
    load_config,
    load_secrets,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.profiles import ProfileCompatibility, closed_profiles
from remote_agents.ports.agent_activity import AgentActivity
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
    parser = argparse.ArgumentParser(
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
    if arguments.command == "install-agent-hooks":
        # --settings names the file to operate on, and --activity-dir the spool the installed
        # command will write to. Both default to the owner's real ones and exist so that the
        # live drill can drive a real agent end to end without going near either.
        settings_path = arguments.settings or default_settings_path(Path.home())
        try:
            outcome = (
                remove_agent_hooks(settings_path)
                if arguments.remove
                else install_agent_hooks(settings_path, activity_directory=arguments.activity_dir)
            )
        except HookInstallError as error:
            print(error, file=sys.stderr)
            return 1
        print(outcome.summary)
        return 0
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
        registry = load_registry(config.registry_path)
        discovered = discover_projects(config.dev_root)
        catalogue = ProjectCatalogueProvider(config.registry_path, config.dev_root).refresh()
        profiles = probe_profiles(
            closed_profiles(),
            resolve=lambda executable: _resolve_profile_executable(executable, paths.home),
        )
        result = production_doctor(
            core_ready=registry.error is None,
            database_ready=database_is_ready(config.database_path),
            tmux_ready=_command_succeeds(("tmux", "-L", "remote-agents", "-V")),
            tmux_console_ready=_console_features_available(paths.home),
            telegram_ready=_telegram_credentials_are_private(paths),
            service_ready=_command_succeeds(
                ("systemctl", "--user", "is-active", "--quiet", "remote-agents.service")
            ),
            profiles=profiles,
            registered_projects=len(registry.projects),
            discovered_projects=len(discovered),
            catalogue_projects=len(catalogue.catalogue),
            # Carried on the healthy path too, so a green report says the config *was*
            # compared rather than leaving the operator to infer it from the absence of a
            # complaint. Silence and a passed check look identical otherwise.
            config_drift=drift,
        )
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
            created = _project_creator(config).create(
                CreateProjectCommand(arguments.area.strip(), arguments.name.strip())
            )
        except (ConfigError, ProjectCreationError) as error:
            print(error, file=sys.stderr)
            return 1
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
        paths = ProductionPaths.for_home(Path.home())
        config = _private_state_config(arguments.config, paths)
        paths.ensure_directories()
        paths.require_private_environment()
        connection = paths.open_database(open_database, migrations=MIGRATIONS)
        try:
            asyncio.run(
                _serve_with_reconciliation(
                    load_secrets(),
                    _private_boundary(config, connection, paths),
                    serve_runner,
                    _RECONCILE_INTERVAL_SECONDS,
                    config.activity_poll_seconds,
                )
            )
        finally:
            connection.close()
    if arguments.command is None:
        # The bare name was unclaimed — no arguments fell through every branch above and
        # exited 0 silently — so this claims it for the one thing a bare invocation can
        # mean: enter the console.
        return _enter_console()
    return 0


@dataclass(frozen=True, slots=True)
class LocalRuntime:
    """The terminal and profile availability every local surface composes identically."""

    terminal: TmuxTerminal
    profiles: tuple[ProfileAvailability, ...]
    # What the probe actually observed, before either surface narrowed it. `profiles` above
    # is the Telegram wizard's type and the local surface converts it back again -- carried
    # separately rather than replaced because both narrowings are still in use, and merging
    # them is sub-plan 4's job (it has a recorded regression to avoid). `Backend` takes this
    # one, so the backend states what was seen and each surface decides what to say.
    compatibility: tuple[ProfileCompatibility, ...]
    # The gateway the terminal wraps, carried separately so the composition root can wire
    # console capabilities (client switching) without widening the terminal port for a
    # concern that is presentation, not lifecycle.
    gateway: TmuxGateway


def _local_runtime(config, paths: ProductionPaths, project_paths) -> LocalRuntime:
    """Compose the one tmux terminal and profile probe that every surface shares."""
    definitions = closed_profiles()
    compatibility = probe_profiles(
        definitions,
        resolve=lambda executable: _resolve_profile_executable(executable, paths.home),
    )
    profiles = tuple(
        ProfileAvailability(str(result.profile_id), result.available, result.reason)
        for result in compatibility
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
    allowed_environment = {
        name: os.environ[name]
        for name in ("HOME", "LANG", "LC_ALL", "PATH", "TERM")
        if name in os.environ
    }
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
    return LocalRuntime(terminal, profiles, tuple(compatibility), gateway)


def compose_backend(
    config,
    connection,
    paths: ProductionPaths,
    *,
    projects: ProjectCatalogueProvider | None = None,
    runtime: LocalRuntime | None = None,
    locks=None,
    hide_in_console=None,
    activity_feed=None,
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
            SQLiteSessionStore(connection),
            runtime.terminal,
            locks=locks,
            hide_in_console=hide_in_console,
        ),
        projects=_project_creator(config),
        conversations=_conversation_service(projects.paths),
        catalogue=catalogue,
        refresh_catalogue=lambda: projects.refresh().catalogue,
        # The domain's record of what was probed, not either surface's narrowing of it —
        # see `Backend.profiles` for why the two narrowings must not be merged in passing.
        profiles=runtime.compatibility,
        capture=runtime.terminal.capture,
        activity_feed=activity_feed,
        max_label_length=config.max_label_length,
    )


def _private_boundary(config, connection, paths: ProductionPaths) -> ServiceComposition:
    projects = ProjectCatalogueProvider(config.registry_path, config.dev_root)
    catalogue = projects.refresh().catalogue
    project_paths = projects.paths
    runtime = _local_runtime(config, paths, project_paths)
    terminal = runtime.terminal
    conversations = _conversation_service(project_paths)
    secrets = load_secrets()
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
    return ServiceComposition(
        PrivateBotBoundary(
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
            catalogue=catalogue,
            profiles=runtime.profiles,
            project_page_size=config.project_page_size,
            max_label_length=config.max_label_length,
            launcher=SessionService(store, terminal, locks=locks, hide_in_console=console.hide),
            conversations=conversations,
            creator=_project_creator(config),
            capture=terminal.capture,
            catalogue_source=lambda: projects.refresh().catalogue,
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
    paths.ensure_directories()
    paths.open_database(open_database, migrations=MIGRATIONS).close()
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
    from remote_agents.adapters.tui.context import FEED_LIMIT, ProfileChoice, TuiContext

    projects = ProjectCatalogueProvider(config.registry_path, config.dev_root)
    catalogue = projects.refresh().catalogue
    runtime = _local_runtime(config, paths, projects.paths)

    open_in_console = None
    console_sync = None
    console_flash = None
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
        # The stop paths ask the console to step out of the way before a pane is destroyed.
        # Wired only where a composer exists: elsewhere `SessionService` keeps the destruction
        # contract it has always had. The bot builds a composer of its own for this one
        # operation (see `_private_boundary`), so both writers now hide before destroying;
        # what still reaches `sync` is a hide that timed out, a degraded console, or a pane
        # that ended without either writer asking.
        hide_in_console = composer.hide

    return TuiContext(
        launcher=SessionService(
            SQLiteSessionStore(connection), runtime.terminal, hide_in_console=hide_in_console
        ),
        creator=_project_creator(config),
        profiles=tuple(
            # A reason only travels with an *unavailable* profile. `ProfileCompatibility`
            # uses `reason` for two things -- why a profile is blocked, and a note about a
            # probe that did not answer -- while `ProfileChoice` reads any reason as
            # blocking and refuses to construct alongside `available=True`. Passing it
            # through unconditionally meant a version probe that merely timed out took the
            # whole local surface down with `an available profile has no blocking reason`.
            ProfileChoice(
                profile.profile_id,
                profile.available,
                None if profile.available else profile.reason,
            )
            for profile in runtime.profiles
        ),
        refresh_catalogue=lambda: projects.refresh().catalogue,
        attach_argv=lambda session_id: attach_argv(SessionId.parse(session_id)),
        max_label_length=config.max_label_length,
        catalogue=catalogue,
        # The same capture the service hands the bot. Redactions default to the empty set
        # the bot also uses -- no configuration key sources them today.
        capture=runtime.terminal.capture,
        conversations=_conversation_service(projects.paths),
        open_in_console=open_in_console,
        console_sync=console_sync,
        # A reader of the durable observation table, never a drainer: consuming the spool
        # would starve the phone's notifications (see Task 5.2's correction note).
        activity_feed=lambda: SQLiteActivityStore(connection).recent(limit=FEED_LIMIT),
        console_flash=console_flash,
        console_recovery=console_recovery,
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


def _telegram_credentials_are_private(paths: ProductionPaths) -> bool:
    """Verify only the private credential-file boundary; never read or print its values."""
    try:
        paths.require_private_environment()
    except ConfigError:
        return False
    return True


def _load_private_telegram_secrets(paths: ProductionPaths) -> TelegramSecrets:
    """Read the checked private EnvironmentFile for this read-only metadata audit."""
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
