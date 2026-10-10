"""The git boundary: what a session worktree needs to ask of one checkout, and nothing more.

The port names three writes and stops there. Creating a worktree, removing one and deleting a
branch git already calls merged are the whole of what remote-agents does to the owner's
repositories; a forced removal and a forced branch delete are not verbs here, so no adapter
can be asked for one (the `host_remote_control` precedent: a destructive verb the port cannot
say is a verb no caller can reach).

Every call runs outside the process and may fail or hang. A refusal from git arrives as
`GitRefused` carrying git's own one-line reason; a call that outlives its bound arrives as
`GitTimeout`. Both are `GitError`, so a caller that only needs "it did not happen" catches one
name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class GitError(Exception):
    """A git call did not do what was asked. `str()` is one line, safe to show the owner."""


class GitRefused(GitError):
    """git ran and exited non-zero. The message is the first line git wrote to stderr."""


class GitTimeout(GitError):
    """git did not finish inside its bound; the process was killed and reaped."""


@dataclass(frozen=True, slots=True)
class GitHead:
    """Where a checkout stands. `branch` is None when HEAD is detached."""

    commit: str
    branch: str | None


@dataclass(frozen=True, slots=True)
class GitSummary:
    """A checkout's changes against a base, counted the way `git diff --numstat` counts them.

    `files`, `added` and `removed` cover tracked files, working tree against `base`. `ahead`
    is how many commits HEAD has that `base` does not. `dirty` is whether `git status` lists
    anything at all, untracked files included.
    """

    branch: str | None
    files: int
    added: int
    removed: int
    ahead: int
    dirty: bool


class GitCheckout(Protocol):
    """Bounded git calls against one repository or worktree."""

    async def top_level(self, path: Path) -> Path | None:
        """The top level of the work tree holding `path`, or None when it is in none."""
        ...

    async def head(self, path: Path) -> GitHead:
        """The commit and branch checked out at `path`."""
        ...

    async def add_worktree(self, repo: Path, path: Path, branch: str, commit: str) -> None:
        """Create `path` as a worktree of `repo` on a new `branch` at `commit`."""
        ...

    async def remove_worktree(self, repo: Path, path: Path) -> None:
        """Remove the worktree at `path`. git refuses one holding changes, and so does this."""
        ...

    async def delete_merged_branch(self, repo: Path, branch: str) -> None:
        """Delete `branch` only if git calls it merged; an unmerged branch is refused."""
        ...

    async def is_ancestor(self, repo: Path, commit: str, ref: str) -> bool:
        """Whether `commit` is reachable from `ref`."""
        ...

    async def status_lines(self, path: Path) -> tuple[str, ...]:
        """`git status --porcelain` entries at `path`, each `XY <path>`, untracked included."""
        ...

    async def diff_summary(self, path: Path, base: str) -> GitSummary:
        """The checkout at `path` measured against `base`."""
        ...
