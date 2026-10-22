"""Where a project's handoff envelopes live: the top level of the git checkout holding it.

The planning plugin writes envelopes and reads `request.json` under the repository's top level,
whatever directory its session was started in (coder-plugins DEC-029). A project registered at a
subdirectory of a checkout therefore shares that checkout's one handoff directory, and a project
in no checkout has none: such a project is never rolled over.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def acceptable_root(value: object) -> Path | None:
    """The reported root `value` names, or None: the one rule on write and on read.

    It must be an absolute path. Taken through `realpath`, it must be a directory this user owns
    that is its own git top level (`handoff_root(p) == p`). Anything else, including a value
    that cannot be resolved or examined, is None.
    """
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        return None
    try:
        resolved = Path(os.path.realpath(value))
        status = os.stat(resolved)
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
            return None
        return resolved if handoff_root(resolved) == resolved else None
    except (OSError, ValueError):
        return None


def handoff_root(project_dir: Path | None) -> Path | None:
    """The nearest directory at or above `project_dir` holding a `.git` (a directory, or the
    file a worktree or submodule has), or None when there is none or no project directory."""
    if project_dir is None:
        return None
    for candidate in (project_dir, *project_dir.parents):
        if (candidate / ".git").exists():
            return candidate
    return None
