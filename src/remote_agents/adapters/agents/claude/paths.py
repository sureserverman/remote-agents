"""Where Claude Code files a working directory's conversations."""

from __future__ import annotations

from pathlib import Path


def project_directory_name(cwd: Path) -> str:
    """`~/.claude/projects/<this>` for a session started in `cwd`: the resolved path with every
    character that is not an ASCII letter or digit written as `-`.

    Not "/" alone. Measured 2026-10-10 against 388 recorded working directories: every one
    whose transcript directory exists is named this way (`/home/user/.cache/x` is
    `-home-user--cache-x`, and `_` becomes `-` too). Every session worktree sits under
    `~/.local/state`, so the slash-only rule never found one (BL-116).
    """
    return "".join(c if c.isascii() and c.isalnum() else "-" for c in str(cwd))
