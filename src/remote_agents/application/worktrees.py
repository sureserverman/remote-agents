"""A session's own worktree: when it is offered, and how one is made, all or nothing (BL-116).

**Offered only on contention.** A second session on a project already being worked in is the
case the mode exists for; a lone session gets today's one-press launch. The offer also needs
the project folder to be the top level of its repository: a worktree is a whole checkout, and
a session bound to one works at that checkout's top (`SessionFolders`), so a project that is a
subfolder of its repository would start a level too high. Such a project is not offered the
mode rather than given a folder that does not match its own.

**Made all or nothing.** The branch, the folder, the copied files and the two rows are four
steps; any one failing undoes the earlier ones -- the copies deleted, the folder removed, the
branch deleted -- each through git's own unforced verbs. A branch that already existed is
refused before anything is made, so the undo can never delete a branch this project did not
create. A launch the terminal then refuses before any pane exists discards the worktree the
same way (`discard`); one whose pane started keeps it, since the agent may have worked there,
and the cleanup pass removes it once its session has ended and its branch is merged into the
base branch it was cut from (one cut from a detached HEAD is listed, never removed).

**Copies are untracked files only, never followed through a link.** A fresh worktree holds
every tracked file at the base commit, so a listed file already present there is tracked and
keeps its committed content. The config loader refuses secrets and git's own state by name;
the copier refuses them again (a hand-built config never passed the loader) and is the only
guard against symlinks: a source or destination with any symlinked component is skipped, only
regular files are copied, and both ends must resolve inside their checkout. Trust is never
written for the new folder (DEC-016): the agent asks, or inherits, exactly as it would anyway.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import stat
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from remote_agents.domain.models import ProjectId, SessionId, SessionRecord, SessionState
from remote_agents.ports.git_checkout import GitCheckout, GitError
from remote_agents.ports.worktree_store import Worktree, WorktreeStore

_LOG = logging.getLogger(__name__)

NEVER_COPIED = re.compile(
    r"^(\.env(\..*)?|\.envrc|\.netrc|\.npmrc|\.pypirc|.*\.pem|\.venv|\.git)$", re.IGNORECASE
)
"""The path components no copy may name, the same rule `config` loads the list under. Spelled
twice because neither module may import the other; `test_worktree_create` pins the two."""

BRANCH_PREFIX = "ra/"
"""A session worktree's branch is this plus the first eight characters of its session id."""

_COPY_MAX_BYTES = 1024 * 1024
"""A copied file is an instruction or settings file; anything larger is skipped, not copied."""

_CONTENDING = frozenset(
    {
        SessionState.STARTING,
        SessionState.UNTRUSTED,
        SessionState.RUNNING,
        SessionState.STOP_REQUESTED,
    }
)
"""The states of a session with an agent at work in its folder: what makes a launch contended.
Not FAILED (a pane that recovers is re-read to RUNNING), PRESERVED (its pane has exited) or
ORPHANED; an offer kept up by a record with no agent behind it would be no contention at all.
A slow agent recorded FAILED while its pane still comes up is missed until the next listing
re-reads it to RUNNING; in that window the launch is a plain one, which is today's behaviour."""


class WorktreeCreateFailed(Exception):
    """No worktree was made, and nothing part-made remains. `str()` is one line for the owner."""


class _Sessions(Protocol):
    async def list(
        self, states: Collection[SessionState] | None = None
    ) -> Sequence[SessionRecord]: ...


@dataclass
class _Made:
    """What a creation has done so far, so an undo touches exactly that."""

    worktree: bool = False
    written: list[str] = field(default_factory=list)


class WorktreeService:
    """The offer and the creation, over the git boundary and the worktree rows."""

    def __init__(
        self,
        *,
        git: GitCheckout,
        store: WorktreeStore,
        sessions: _Sessions,
        project_paths: Mapping[ProjectId, Path],
        directory: Path,
        copy_for: Callable[[Path], Sequence[str]],
    ) -> None:
        self._git = git
        self._store = store
        self._sessions = sessions
        # The catalogue's live view, as `SessionFolders` holds it.
        self._project_paths = project_paths
        self._directory = directory
        self._copy_for = copy_for

    async def offered(self, project_id: ProjectId) -> bool:
        """Whether a launch on this project offers its own worktree. Never raises: a fault
        reading either answer withholds the offer, and the launch stays an ordinary one."""
        try:
            live = await self._sessions.list(_CONTENDING)
            if not any(record.project_id == project_id for record in live):
                return False
            return await self._repository(project_id) is not None
        except Exception:
            _LOG.exception("the worktree offer for %s could not be decided", project_id)
            return False

    async def _repository(self, project_id: ProjectId) -> Path | None:
        """The project's repository when the project folder is its top level, else None."""
        project = self._project_paths.get(project_id)
        if project is None:
            return None
        resolved = project.resolve()
        top = await self._git.top_level(resolved)
        return top if top is not None and top.resolve() == resolved else None

    async def create(self, session_id: SessionId, project_id: ProjectId) -> Worktree:
        """Make, copy into, record and bind the session's worktree, or raise
        `WorktreeCreateFailed` having undone every step taken."""
        branch = f"{BRANCH_PREFIX}{str(session_id)[:8]}"
        path = self._directory / str(project_id) / str(session_id)
        # Nothing is made before this block ends, so a failure in it has nothing to undo.
        try:
            repo = await self._repository(project_id)
            if repo is None:
                raise WorktreeCreateFailed("the project folder is not the top of a git repository")
            head = await self._git.head(repo)
            if await self._git.has_branch(repo, branch):
                raise WorktreeCreateFailed(f"branch {branch} already exists")
            if os.path.lexists(path):
                raise WorktreeCreateFailed(f"{path} already exists")
        except WorktreeCreateFailed:
            raise
        except Exception as error:
            raise WorktreeCreateFailed(_one_line(error)) from None
        made = _Made()
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            made.worktree = True
            await self._git.add_worktree(repo, path, branch, head.commit)
            copied = _copy(
                repo, path, self._copy_for(self._project_paths[project_id]), made.written
            )
            worktree = Worktree(
                path=path,
                project_id=project_id,
                branch=branch,
                base_branch=head.branch,
                base_commit=head.commit,
                copied=copied,
                created_at=datetime.now(UTC),
                removed_at=None,
            )
            # Last, and one transaction with no await point inside the store's body: once it
            # has run there is nothing left to fail or be cancelled, so a row never outlives
            # an undo.
            await self._store.record(worktree, bound_to=session_id)
        except asyncio.CancelledError:
            # Shielded: a cancel that ended the creation must not also end its undo.
            await asyncio.shield(self._undo(repo, path, branch, made))
            raise
        except Exception as error:
            _LOG.warning("worktree for %s not created: %s", session_id, error)
            leftover = await asyncio.shield(self._undo(repo, path, branch, made))
            reason = _one_line(error)
            raise WorktreeCreateFailed(
                reason if not leftover else f"{reason} (left behind: {leftover})"
            ) from None
        return worktree

    async def _undo(self, repo: Path, path: Path, branch: str, made: _Made) -> str:
        """Undo a creation's steps, latest first. Answers what could not be undone, or ''."""
        leftover = []
        for relative in reversed(made.written):
            with contextlib.suppress(FileNotFoundError, NotADirectoryError):
                os.unlink(path.joinpath(*PurePosixPath(relative).parts))
        if made.worktree and os.path.lexists(path):
            try:
                await self._git.remove_worktree(repo, path)
            except GitError as error:
                # Never forced here. A `worktree add` killed at its timeout leaves its folder
                # locked, which only a doubly forced remove clears: the owner is told how.
                _LOG.warning("half-made worktree %s not removed: %s", path, error)
                leftover.append(
                    f"folder {path}; remove it with `git -C {repo} worktree remove "
                    f"--force --force {path}`"
                )
        if made.worktree:
            # The branch was checked absent before `worktree add`, so one present now is ours.
            try:
                if await self._git.has_branch(repo, branch):
                    await self._git.delete_merged_branch(repo, branch)
            except GitError as error:
                _LOG.warning("half-made branch %s not deleted: %s", branch, error)
                leftover.append(f"branch {branch}")
        if made.worktree:
            # The per-project folder made for it, when nothing else is in it.
            with contextlib.suppress(OSError):
                path.parent.rmdir()
        return ", ".join(leftover)

    async def discard(self, session_id: SessionId) -> str:
        """Remove the worktree a launch made when the terminal refused before any pane
        existed: nothing ran there, so the copies are unchanged and the branch is at its base.
        Marked removed only once nothing of it is left: a branch git would not delete keeps
        the row live, so the cleanup pass still finds it when the session has ended. Touches
        only a folder this session alone is bound to. Answers what was left, or ''."""
        worktree = await self._store.folder_for(session_id)
        if worktree is None or worktree.removed_at is not None:
            return ""
        if await self._store.bound_sessions(worktree.path) != (session_id,):
            return f"folder {worktree.path} (another session is bound to it)"
        repo = await self._repository(worktree.project_id)
        if repo is None:
            return f"folder {worktree.path}"
        made = _Made(worktree=True, written=list(worktree.copied))
        leftover = await asyncio.shield(self._undo(repo, worktree.path, worktree.branch, made))
        if not leftover and not os.path.lexists(worktree.path):
            await self._store.mark_removed(worktree.path, at=datetime.now(UTC))
        return leftover


def _one_line(error: BaseException) -> str:
    text = str(error).strip()
    return (text.splitlines()[0] if text else type(error).__name__)[:200]


def _copy(repo: Path, tree: Path, entries: Sequence[str], written: list[str]) -> dict[str, str]:
    """Copy each listed untracked regular file from `repo` into `tree`; answer path -> sha256.

    Each path is appended to `written` before its file is created, so an undo after a failure
    part-way through one file still deletes it."""
    copied: dict[str, str] = {}
    for entry in entries:
        parts = PurePosixPath(entry).parts
        if (
            not parts
            or PurePosixPath(entry).is_absolute()
            or ".." in parts
            or any(NEVER_COPIED.match(part) for part in parts)
        ):
            _LOG.warning("worktree copy skipped %r: not a plain path inside the checkout", entry)
            continue
        # As git names it, so a later `status` line and this key are the same string.
        key = PurePosixPath(*parts).as_posix()
        source = repo.joinpath(*parts)
        if not _plain_path(repo, parts, final_must_exist=True):
            continue
        target = tree.joinpath(*parts)
        if not _plain_path(tree, parts, final_must_exist=False):
            _LOG.warning("worktree copy skipped %r: a symlink in the worktree's path", entry)
            continue
        if os.path.lexists(target):
            # A fresh worktree holds exactly the tracked files: this one is tracked.
            continue
        data, mode = _read_regular(source)
        if data is None:
            continue
        if not (
            source.resolve().is_relative_to(repo.resolve())
            and target.parent.resolve().is_relative_to(tree.resolve())
        ):
            _LOG.warning("worktree copy skipped %r: resolves outside its checkout", entry)
            continue
        current = tree
        for part in parts[:-1]:
            current = current / part
            if not os.path.lexists(current):
                current.mkdir(mode=0o700)
        written.append(key)
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode & 0o777
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        copied[key] = hashlib.sha256(data).hexdigest()
    return copied


def _plain_path(root: Path, parts: Sequence[str], *, final_must_exist: bool) -> bool:
    """Whether no component of `root/parts` is a symlink, and every parent that exists is a
    folder. With `final_must_exist`, the last component must exist too."""
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return not final_must_exist
        if stat.S_ISLNK(mode):
            return False
        if index < len(parts) - 1 and not stat.S_ISDIR(mode):
            return False
    return True


def _read_regular(source: Path) -> tuple[bytes | None, int]:
    """A regular file's bytes and mode, read without following a link; None otherwise."""
    try:
        # Non-blocking: a FIFO by a listed name would otherwise hang the open, and with it
        # the event loop. A regular file reads the same either way.
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None, 0
    try:
        # Typed on the raw descriptor: `fdopen` raises on a directory and keeps it open.
        info = os.fstat(descriptor)
        data = None
        if stat.S_ISREG(info.st_mode):
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                data = handle.read(_COPY_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if data is None or len(data) > _COPY_MAX_BYTES:
        _LOG.warning("worktree copy skipped %s: not a regular file under the cap", source)
        return None, 0
    return data, info.st_mode
