"""Bounded Git checks for destination routing; never execute envelope text as a command."""

import asyncio
import subprocess
from pathlib import Path

from remote_agents.adapters.workflow.roots import acceptable_root


async def destination_branch(root: Path) -> str | None:
    return await asyncio.to_thread(_branch, root)


def _branch(root: Path) -> str | None:
    if acceptable_root(str(root)) != root:
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--show-toplevel",
                "--symbolic-full-name",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 2:
        return None
    if Path(lines[0]).resolve() != root or not lines[1].startswith("refs/heads/"):
        return None
    return lines[1].removeprefix("refs/heads/")
