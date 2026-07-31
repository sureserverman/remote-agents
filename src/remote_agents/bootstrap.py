"""Composition root for the private Telegram control-plane service."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from remote_agents.adapters.projects.discovery import discover_projects
from remote_agents.adapters.projects.registry import load_registry
from remote_agents.adapters.sqlite.database import database_is_ready, restore_database
from remote_agents.adapters.telegram.service import run_private_bot
from remote_agents.adapters.tmux.profiles import probe_profiles
from remote_agents.application.doctor import doctor, profile_doctor
from remote_agents.application.project_catalog import build_catalogue
from remote_agents.config import ConfigError, TelegramSecrets, load_config, load_secrets
from remote_agents.domain.profiles import closed_profiles, qualified_profiles
from remote_agents.production import ProductionPaths


def main(
    argv: list[str] | None = None,
    *,
    serve_runner: Callable[[TelegramSecrets], Awaitable[None]] = run_private_bot,
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
            asyncio.run(serve_runner(load_secrets()))
        finally:
            connection.close()
    return 0
