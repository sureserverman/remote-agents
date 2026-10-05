"""Where a project's handoff envelopes live: the top level of the git checkout holding it.

The planning plugin writes envelopes and reads `request.json` under the repository's top level,
whatever directory its session was started in (coder-plugins DEC-029). A project registered at a
subdirectory of a checkout therefore shares that checkout's one handoff directory, and a project
in no checkout has none: such a project is never rolled over.
"""

from __future__ import annotations

from pathlib import Path


def handoff_root(project_dir: Path | None) -> Path | None:
    """The nearest directory at or above `project_dir` holding a `.git` (a directory, or the
    file a worktree or submodule has), or None when there is none or no project directory."""
    if project_dir is None:
        return None
    for candidate in (project_dir, *project_dir.parents):
        if (candidate / ".git").exists():
            return candidate
    return None
