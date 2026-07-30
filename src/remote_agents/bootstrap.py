"""Composition root for the application.

The runtime composition is introduced after the core and adapter stages. This temporary
help-only command keeps the installed package executable without leaking policy into the
package entry point.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from remote_agents.adapters.projects.discovery import discover_projects
from remote_agents.adapters.projects.registry import load_registry
from remote_agents.adapters.sqlite.database import database_is_ready
from remote_agents.adapters.tmux.profiles import probe_profiles
from remote_agents.application.doctor import doctor, profile_doctor
from remote_agents.application.project_catalog import build_catalogue
from remote_agents.config import load_config
from remote_agents.domain.profiles import closed_profiles


def main() -> int:
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
    arguments = parser.parse_args()
    if arguments.command == "doctor":
        if arguments.profiles:
            result = profile_doctor(probe_profiles(closed_profiles()))
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
    return 0
