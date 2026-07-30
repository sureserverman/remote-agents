"""Composition root for the application.

The runtime composition is introduced after the core and adapter stages. This temporary
help-only command keeps the installed package executable without leaking policy into the
package entry point.
"""

from __future__ import annotations

import argparse


def main() -> int:
    """Run the current composition-root command-line interface."""
    parser = argparse.ArgumentParser(
        prog="remote-agents",
        description="Private Telegram control plane for local agent sessions.",
    )
    parser.parse_args()
    return 0
