"""Composition root for the private Telegram control-plane service."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
from collections.abc import Awaitable, Callable
from hashlib import sha256
from pathlib import Path

from remote_agents.adapters.projects.discovery import discover_projects
from remote_agents.adapters.projects.registry import load_registry
from remote_agents.adapters.sqlite.database import database_is_ready, restore_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.telegram.service import PrivateBotBoundary, run_private_bot
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import build_launch_profile, probe_profiles
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.application.doctor import doctor, profile_doctor
from remote_agents.application.project_catalog import build_catalogue
from remote_agents.application.services import SessionService
from remote_agents.config import ConfigError, TelegramSecrets, load_config, load_secrets
from remote_agents.domain.models import ProjectId
from remote_agents.domain.profiles import closed_profiles, qualified_profiles
from remote_agents.production import ProductionPaths


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
    arguments = parser.parse_args(argv)
    if arguments.command == "doctor":
        if arguments.profiles:
            result = profile_doctor(
                probe_profiles(closed_profiles(), qualifications=qualified_profiles())
            )
            print(json.dumps(result, sort_keys=True) if arguments.json else result)
            return 0
        if arguments.config is None:
            doctor_parser.error("--config is required unless --profiles is selected")
        config = load_config(arguments.config)
        registry = load_registry(config.registry_path)
        discovered = discover_projects(config.dev_root)
        catalogue = build_catalogue(registry.projects, discovered, registry_error=registry.error)
        result = doctor(
            database_ready=database_is_ready(config.database_path),
            registered_projects=len(registry.projects),
            discovered_projects=len(discovered),
            catalogue_projects=len(catalogue),
            registry_error=registry.error,
            fake_terminal=arguments.fake_terminal,
        )
        print(json.dumps(result, sort_keys=True) if arguments.json else result)
    if arguments.command == "restore-database":
        restore_database(arguments.database, arguments.backup)
        print("database restored")
    if arguments.command == "serve":
        config = load_config(arguments.config)
        paths = ProductionPaths.for_home(Path.home())
        if config.database_path != paths.database_path:
            raise ConfigError(
                "production database path must be "
                f"{paths.database_path}; refusing to write outside the private state directory"
            )
        paths.ensure_directories()
        paths.require_private_environment()
        connection = paths.open_database()
        try:
            asyncio.run(serve_runner(load_secrets(), _private_boundary(config, connection, paths)))
        finally:
            connection.close()
    return 0


def _private_boundary(config, connection, paths: ProductionPaths) -> PrivateBotBoundary:
    registry = load_registry(config.registry_path)
    discovered = discover_projects(config.dev_root)
    catalogue = build_catalogue(registry.projects, discovered, registry_error=registry.error)
    project_paths = {
        ProjectId(_opaque_id(project.path)): project.path.resolve(strict=True)
        for project in (*registry.projects, *discovered)
    }
    definitions = closed_profiles()
    compatibility = probe_profiles(
        definitions,
        qualifications=qualified_profiles(),
        resolve=lambda executable: _resolve_profile_executable(executable, paths.home),
    )
    profiles = tuple(
        ProfileAvailability(str(result.profile_id), result.status == "QUALIFIED")
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
        if result.status == "QUALIFIED" and executable is not None:
            definition = definitions_by_id[result.profile_id]
            profile_factories[result.profile_id] = _profile_factory(
                definition, executable, allowed_environment
            )
    terminal = TmuxTerminal(
        TmuxGateway("remote-agents", AsyncTmuxRunner(), intent_directory=paths.intent_directory),
        project_paths,
        {},
        startup_timeout=20,
        profile_factories=profile_factories,
    )
    secrets = load_secrets()
    return PrivateBotBoundary(
        secrets.owner_user_id,
        secrets.owner_chat_id,
        catalogue=catalogue,
        profiles=profiles,
        launcher=SessionService(SQLiteSessionStore(connection), terminal),
        capture=terminal.capture,
    )


def _opaque_id(path: Path) -> str:
    return sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()[:24]


def _profile_factory(definition, executable: Path, environment: dict[str, str]):
    return lambda session_id: build_launch_profile(definition, executable, session_id, environment)


def _resolve_profile_executable(executable: str, home: Path) -> Path | None:
    for candidate in (
        home / ".local" / "bin" / executable,
        *sorted((home / ".nvm" / "versions" / "node").glob(f"*/bin/{executable}")),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    resolved = shutil.which(executable)
    return Path(resolved) if resolved is not None else None
