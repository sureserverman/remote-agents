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

from collections.abc import Mapping
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
    is how many commits HEAD has that `base` does not. `dirty` is whether `git status` (ignored
    files not asked for) lists
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

    async def worktree_head(self, repo: Path, path: Path) -> GitHead | None:
        """What git records the worktree at `path` as having checked out, read from `repo`'s
        own worktree list; None when git holds no entry for `path`.

        Asked of the repository, not the folder, so it answers for a folder already gone: git
        keeps a missing folder's entry, its HEAD and its reflog, until `worktree remove` or a
        prune clears them -- and with them any commit only that HEAD reaches."""
        ...

    async def has_branch(self, repo: Path, branch: str) -> bool:
        """Whether `refs/heads/<branch>` exists in `repo`: asked before a branch is made, so an
        undo deletes only a branch this project created."""
        ...

    async def add_worktree(self, repo: Path, path: Path, branch: str, commit: str) -> None:
        """Create `path` as a worktree of `repo` on a new `branch` at `commit`."""
        ...

    async def remove_worktree(self, repo: Path, path: Path) -> None:
        """Remove the worktree at `path`, never forced.

        git refuses one holding modified or untracked files. **It deletes ignored files without
        asking** (measured on git 2.43.0), so a caller deciding whether a folder may go reads
        `status_lines`, which lists them, first."""
        ...

    async def delete_merged_branch(self, repo: Path, branch: str) -> None:
        """`branch -d`: refused unless git calls `branch` merged.

        git judges "merged" against the main checkout's current HEAD (or the branch's
        upstream), not against any base this project recorded, so it is a backstop: a caller
        decides with `is_ancestor` against the base first."""
        ...

    async def is_ancestor(self, repo: Path, commit: str, ref: str) -> bool:
        """Whether `commit` is reachable from `ref`.

        Pass branches fully qualified (`refs/heads/main`): a tag sharing a branch's short name
        wins when git resolves the short one."""
        ...

    async def unmerged_history(self, repo: Path, path: Path, branch: str, base: str) -> int:
        """How many commits that only the worktree at `path` and `branch`'s reflog still reach
        are not reachable from `refs/heads/<base>`.

        Removing a worktree deletes its own refs (`refs/worktree/*`, `refs/bisect/*`,
        `refs/rewritten/*`) and its HEAD reflog, and `branch -d` deletes the branch's reflog.
        A commit made and then reset away, or made on a detached HEAD and left, is reachable
        only from those -- so the branch tip alone saying "merged" is not enough. Asked of the
        repository, so it answers for a folder already gone.

        Raises `GitRefused` rather than answer from part of that set: an entry git holds whose
        admin folder cannot be placed, a reflog that cannot be read or is longer than the
        adapter reads, a ref naming an object git no longer has. With reflogs off, only the
        last commit reset away is named (by `ORIG_HEAD`); an earlier one was already reachable
        from nothing."""
        ...

    async def status_lines(self, path: Path) -> tuple[str, ...]:
        """Every `git status --porcelain` entry at `path`, each `XY <path>`: modified, untracked
        (`??`) and ignored (`!!`), every untracked and ignored *file* listed on its own. A tracked
        file marked `assume-unchanged` or `skip-worktree` is listed too, as `h? <path>` /
        `S? <path>` (its `ls-files -v` tag), since `status` hides its edits. So is any entry
        named `.git` below the top, as `G? <path>`: git skips one that is not a repository it
        can open -- a file, a plain folder, a nested repository gone corrupt -- and `worktree
        remove` deletes it all the same.

        Raises `GitRefused` when the listing is longer than the adapter will read, rather than
        return one missing its tail."""
        ...

    async def merge_base(self, path: Path, one: str, other: str) -> str | None:
        """The best common ancestor of two revisions, or None when they share none."""
        ...

    async def diff_summary(
        self, path: Path, base: str, copies: Mapping[str, str] | None = None
    ) -> GitSummary:
        """The checkout at `path` measured against `base`. The counts are `diff --shortstat`'s,
        so they are read whatever the size of the change. An untracked file named in `copies`
        whose sha256 is still the one recorded is not counted as dirty: it is a file the
        worktree was given, not one the session wrote."""
        ...
