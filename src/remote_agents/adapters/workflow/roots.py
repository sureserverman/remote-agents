"""Where a project's handoff envelopes live: the top level of the git checkout holding it.

The planning plugin writes envelopes and reads `request.json` under the repository's top level,
whatever directory its session was started in (coder-plugins DEC-029). A project registered at a
subdirectory of a checkout therefore shares that checkout's one handoff directory, and a project
in no checkout has none: its sessions roll over only from a root their writer reported
(`acceptable_root`, DEC-131).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def acceptable_root(value: object) -> Path | None:
    """The reported root `value` names, or None: the one rule on write and on read.

    It must be an absolute path. Taken through `realpath`, it must be a directory this user owns
    that is its own git top level (`handoff_root(p) == p`) by a marker git itself would read: a
    real `.git` directory, or a regular `.git` file starting `gitdir:` (a worktree's). A tool shell
    chooses this value, so it is held to more than `handoff_root`'s presence test, which sets a
    session's default root; anything it accepts, that test accepts too. Anything else, including
    a value that cannot be resolved or examined, is None.
    """
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        return None
    try:
        resolved = Path(os.path.realpath(value))
        status = os.stat(resolved)
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
            return None
        if handoff_root(resolved) != resolved or not _real_git_marker(resolved / ".git"):
            return None
        return resolved
    except (OSError, ValueError):
        return None


def _real_git_marker(marker: Path) -> bool:
    """A `.git` directory, or a regular `.git` file starting `gitdir:`; never through a link."""
    status = os.lstat(marker)
    if stat.S_ISDIR(status.st_mode):
        return True
    if not stat.S_ISREG(status.st_mode):
        return False
    descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        return os.read(descriptor, 7) == b"gitdir:"
    finally:
        os.close(descriptor)


def handoff_root(project_dir: Path | None) -> Path | None:
    """The nearest directory at or above `project_dir` holding a `.git` (a directory, or the
    file a worktree or submodule has), or None when there is none or no project directory."""
    if project_dir is None:
        return None
    for candidate in (project_dir, *project_dir.parents):
        if (candidate / ".git").exists():
            return candidate
    return None
