"""Composition root for the application.

The runtime composition is introduced after the core and adapter stages. This temporary
help-only command keeps the installed package executable without leaking policy into the
package entry point.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from remote_agents.application.doctor import doctor
from remote_agents.config import load_config


def main() -> int:
    """Run the current composition-root command-line interface."""
    parser = argparse.ArgumentParser(
        prog="remote-agents",
        description="Private Telegram control plane for local agent sessions.",
    )
    subcommands = parser.add_subparsers(dest="command")
    doctor_parser = subcommands.add_parser("doctor")
    doctor_parser.add_argument("--config", type=Path, required=True)
    doctor_parser.add_argument("--fake-terminal", action="store_true")
    doctor_parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()
    if arguments.command == "doctor":
        config = load_config(arguments.config)
        result = doctor(
            config.dev_root,
            config.registry_path,
            config.database_path,
            fake_terminal=arguments.fake_terminal,
        )
        print(json.dumps(result, sort_keys=True) if arguments.json else result)
    return 0
