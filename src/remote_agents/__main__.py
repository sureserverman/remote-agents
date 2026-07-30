"""Package command-line entry point.

The composition root is introduced with the runtime stage. Keeping this entry point
dependency-free lets packaging and architecture checks run before adapters exist.
"""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    """Return the temporary package-level parser used during foundation work."""
    return argparse.ArgumentParser(
        prog="remote-agents",
        description="Private Telegram control plane for local agent sessions.",
    )


def main() -> int:
    """Run the package command-line interface."""
    build_parser().parse_args()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
