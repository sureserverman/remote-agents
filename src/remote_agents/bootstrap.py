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
from remote_agents.adapters.agents.opencode_sessions import (
    OpenCodeCliRunner,
    OpenCodeSessionCatalogue,
)
from remote_agents.adapters.projects.discovery import discover_projects
from remote_agents.adapters.projects.registry import load_registry
from remote_agents.adapters.projects.registry_writer import RegistryProjectRecorder
from remote_agents.adapters.projects.workspace import FilesystemProjectWorkspace
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
from remote_agents.adapters.tmux.codec import attach_command
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import (
    build_launch_profile,
    build_resume_profile,
    probe_profiles,
)
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.adapters.tui.app import run_local_terminal
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.conversations import ConversationService
from remote_agents.application.doctor import production_doctor, profile_doctor
from remote_agents.application.errors import ProjectCreationError
from remote_agents.application.project_admin import CreateProjectCommand, ProjectCreationService
from remote_agents.application.project_catalog import CatalogProject, build_catalogue
from remote_agents.application.services import SessionService
from remote_agents.config import ConfigError, TelegramSecrets, load_config, load_secrets
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.profiles import closed_profiles
from remote_agents.production import ProductionPaths

_LOG = logging.getLogger(__name__)


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
    arguments = parser.parse_args(argv)
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
        paths = ProductionPaths.for_home(Path.home())
        try:
            config = _private_state_config(arguments.config or paths.config_path, paths)
        except ConfigError as error:
            print(error, file=sys.stderr)
            return 1
        paths.ensure_directories()
        connection = paths.open_database(open_database, migrations=MIGRATIONS)
        try:
            return run_local_terminal(local_context(config, connection, paths))
        finally:
            connection.close()
    if arguments.command == "serve":
        paths = ProductionPaths.for_home(Path.home())
        config = _private_state_config(arguments.config, paths)
        paths.ensure_directories()
        paths.require_private_environment()
        connection = paths.open_database(open_database, migrations=MIGRATIONS)
        try:
            asyncio.run(serve_runner(load_secrets(), _private_boundary(config, connection, paths)))
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


def _private_boundary(config, connection, paths: ProductionPaths) -> PrivateBotBoundary:
    projects = ProjectCatalogueProvider(config.registry_path, config.dev_root)
    catalogue = projects.refresh().catalogue
    project_paths = projects.paths
    runtime = _local_runtime(config, paths, project_paths)
    terminal = runtime.terminal
    conversations = ConversationService(
        ProfileConversationCatalogue(
            {
                ProfileId("claude"): ClaudeSessionCatalogue(project_paths),
                ProfileId("codex"): CodexSessionCatalogue(project_paths, CodexAppServerClient()),
                ProfileId("opencode"): OpenCodeSessionCatalogue(project_paths, OpenCodeCliRunner()),
                ProfileId("cursor-agent"): CursorSessionCatalogue(),
            }
        )
    )
    secrets = load_secrets()
    return PrivateBotBoundary(
        secrets.owner_user_id,
        secrets.owner_chat_id,
        catalogue=catalogue,
        profiles=runtime.profiles,
        project_page_size=config.project_page_size,
        launcher=SessionService(SQLiteSessionStore(connection), terminal),
        conversations=conversations,
        creator=_project_creator(config),
        capture=terminal.capture,
        catalogue_source=lambda: projects.refresh().catalogue,
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


def local_context(config, connection, paths: ProductionPaths) -> TuiContext:
    """Compose the local terminal surface over the same store the service uses."""
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
        attach_command=lambda session_id: attach_command(SessionId.parse(session_id)),
        max_label_length=config.max_label_length,
        catalogue=catalogue,
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
