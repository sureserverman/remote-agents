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
from remote_agents.adapters.sqlite.callback_state_store import SQLiteCallbackStateStore
from remote_agents.adapters.sqlite.chat_view_store import SQLiteChatViewStore
from remote_agents.adapters.sqlite.database import (
    database_is_ready,
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
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.adapters.tmux.codec import attach_argv
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import (
    build_launch_profile,
    build_resume_profile,
    probe_profiles,
)
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.agent_event import spool_from_stdin
from remote_agents.application.activity import PaneQuietWatcher
from remote_agents.application.conversations import ConversationService
from remote_agents.application.doctor import production_doctor, profile_doctor
from remote_agents.application.errors import ProjectCreationError
from remote_agents.application.project_admin import CreateProjectCommand, ProjectCreationService
from remote_agents.application.project_catalog import CatalogProject, build_catalogue
from remote_agents.application.reconcile import ReconciliationService
from remote_agents.application.services import SessionService
from remote_agents.config import ConfigError, TelegramSecrets, load_config, load_secrets
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.profiles import closed_profiles
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
    if composition.quiet_watcher is not None:
        # A separate task rather than another step inside the reconciliation pass: the two
        # answer different questions on different clocks, and a pane capture that hangs must
        # not stop records being reconciled. Nothing is polled before the service is serving,
        # unlike reconciliation -- a first pass at start-up could only establish the baseline
        # the classifier already refuses to report on.
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
    """One pass, which may never raise: this loop runs beside the one that serves the owner.

    The activities this returns are deliberately dropped, and that is a stage boundary rather
    than an oversight: detection is built here and delivery is built next, alongside the hook
    spool's own drain, which is unwired for the same reason. Said out loud because "the service
    computes it" and "the owner is told" are indistinguishable from inside this function, and a
    reader finding a discarded return value has no way to tell a boundary from a bug.
    """
    if composition.quiet_watcher is None:
        return
    try:
        await composition.quiet_watcher.poll()
    except Exception:
        _LOG.exception("activity watch pass failed")


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
        paths = ProductionPaths.for_home(Path.home())
        config = load_config(arguments.config or paths.config_path)
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
            telegram_ready=_telegram_credentials_are_private(paths),
            service_ready=_command_succeeds(
                ("systemctl", "--user", "is-active", "--quiet", "remote-agents.service")
            ),
            profiles=profiles,
            registered_projects=len(registry.projects),
            discovered_projects=len(discovered),
            catalogue_projects=len(catalogue.catalogue),
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
        from remote_agents.adapters.tui.attach import attach_to

        paths = ProductionPaths.for_home(Path.home())
        try:
            config = _private_state_config(arguments.config or paths.config_path, paths)
        except ConfigError as error:
            print(error, file=sys.stderr)
            return 1
        paths.ensure_directories()
        connection = paths.open_database(open_database, migrations=MIGRATIONS)
        request = None
        try:
            request = run_local_terminal(local_context(config, connection, paths))
        except Exception:
            _LOG.exception("the local terminal surface failed")
            print(
                "The terminal surface failed. Any session it started is listed by:\n"
                "tmux -L remote-agents list-sessions",
                file=sys.stderr,
            )
            return 1
        finally:
            connection.close()
        return attach_to(request)
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
    return 0


@dataclass(frozen=True, slots=True)
class LocalRuntime:
    """The terminal and profile availability every local surface composes identically."""

    terminal: TmuxTerminal
    profiles: tuple[ProfileAvailability, ...]


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
    terminal = TmuxTerminal(
        TmuxGateway("remote-agents", AsyncTmuxRunner(), intent_directory=paths.intent_directory),
        project_paths,
        {},
        startup_timeout=20,
        profile_factories=profile_factories,
        resume_profile_factories=resume_profile_factories,
    )
    return LocalRuntime(terminal, profiles)


def _private_boundary(config, connection, paths: ProductionPaths) -> ServiceComposition:
    projects = ProjectCatalogueProvider(config.registry_path, config.dev_root)
    catalogue = projects.refresh().catalogue
    project_paths = projects.paths
    runtime = _local_runtime(config, paths, project_paths)
    terminal = runtime.terminal
    conversations = _conversation_service(project_paths)
    secrets = load_secrets()
    store = SQLiteSessionStore(connection)
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
            catalogue=catalogue,
            profiles=runtime.profiles,
            project_page_size=config.project_page_size,
            max_label_length=config.max_label_length,
            launcher=SessionService(store, terminal),
            conversations=conversations,
            creator=_project_creator(config),
            capture=terminal.capture,
            catalogue_source=lambda: projects.refresh().catalogue,
        ),
        terminal,
        ReconciliationService(store),
        PaneQuietWatcher(store, terminal.capture, quiet_polls=config.activity_quiet_polls),
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
    from remote_agents.adapters.tui.context import ProfileChoice, TuiContext

    projects = ProjectCatalogueProvider(config.registry_path, config.dev_root)
    catalogue = projects.refresh().catalogue
    runtime = _local_runtime(config, paths, projects.paths)
    return TuiContext(
        launcher=SessionService(SQLiteSessionStore(connection), runtime.terminal),
        creator=_project_creator(config),
        profiles=tuple(
            ProfileChoice(profile.profile_id, profile.available, profile.reason)
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
    for line in environment_path.read_text(encoding="utf-8").splitlines():
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
