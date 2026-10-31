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
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        branch = subprocess.run(
            ["git", "-C", str(root), "symbolic-ref", "-q", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = result.stdout.splitlines()
    ref = branch.stdout.strip()
    if result.returncode != 0 or branch.returncode != 0 or len(lines) != 1:
        return None
    if Path(lines[0]).resolve() != root or not ref.startswith("refs/heads/"):
        return None
    return ref.removeprefix("refs/heads/")
