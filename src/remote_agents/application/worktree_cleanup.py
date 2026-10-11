"""Removing a session worktree once nothing in it can be lost (BL-116).

This is the only code that deletes a worktree a session has run in, so a folder goes only when
every guard says it may, each asked fresh on every pass. (The one other removal,
`WorktreeService.discard`, takes back a worktree whose launch was refused before any pane
existed: nothing ran there, and only the session that made it is bound to it.)

- (a) it was cut from a branch, not a detached HEAD -- with no base branch there is nothing to
  call it merged into;
- (b) git records the worktree as still on its own branch, that branch is reachable from the
  base branch, and so is every commit the worktree's own refs and reflogs, and the branch's
  reflog, still reach -- so every commit made in it survives there. The branch tip alone is
  not enough: a commit made on a detached HEAD, on another branch, or reset away is reachable
  only from what `worktree remove` and `branch -d` delete;
- (c) `git status` lists nothing in it but the files it was given at creation, each still byte
  for byte what was copied -- `worktree remove` deletes ignored files without asking, so an
  ignored file is no safer than an untracked one, and an entry named `.git` below the top,
  which `status` never names, keeps it too; and the folder is not itself a link;
- (d) no live session is bound to it and no open rollover names a session that was, since a
  successor reuses its predecessor's folder (DEC-123).

The row is claimed before git is asked, so a resume binding a session into the folder from the
surface's process and this pass removing it exclude each other (`WorktreeStore`). Any guard
saying no, any git refusal and any fault give the claim back and leave the folder; a refusal is
never retried with force -- the port has no forcing verb to retry with. The pass runs in `serve`
alone: its first pass gives back every claim a dead process left, safe only with one per host.

"In use" means a remote-agents session or rollover. An owner's own shell or editor in the
folder is invisible to it: git still refuses a modified or untracked file at removal, but an
ignored file written between the guards and `worktree remove` would go with the folder.

**A folder already gone** -- a launch refused before its pane discards its worktree, and git
may keep a stale entry for it -- has nothing left in it to lose, so (c) is not asked of it; (a),
(b) and (d) are, (b) against the HEAD git's entry still records. `worktree remove` clears that
entry (measured on git 2.43.0), after which `branch -d` can run. Once the folder is gone the
row is marked removed whatever `branch -d` says, and a branch git kept is logged with the
command that deletes it by hand; git's `-d` judges against the main checkout's HEAD, not the
recorded base, so it may refuse a branch (b) already proved merged.

**A removal cut off part-way stays.** A shutdown whose drain times out cancels a `worktree
remove` mid-delete; the folder left behind lists deleted tracked files, so every later pass
reads it dirty and keeps it, and `doctor` lists it for the owner to remove by hand. That fails
toward keeping: what was deleted had already passed every guard.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import stat
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from remote_agents.application.errors import MutationsDraining
from remote_agents.domain.models import ProjectId, SessionId, SessionRecord
from remote_agents.domain.state_machine import TERMINAL_STATES
from remote_agents.ports.git_checkout import GitCheckout, GitError
from remote_agents.ports.rollover_store import Rollover
from remote_agents.ports.worktree_store import Worktree, WorktreeStore

_LOG = logging.getLogger(__name__)

# The reasons a folder is kept, as `doctor` lists them. A git refusal reads `git: <reason>`,
# any other fault `fault: <kind>`.
UNMERGED = "unmerged"
MOVED = "not on its branch"
DIRTY = "dirty"
IN_USE = "in use"
DETACHED_BASE = "detached base"
REMOVABLE = "removable"
"""Every guard lets it go; the next pass removes it."""

NOT_A_REPOSITORY = "git: the project is not catalogued, or not the top of a git repository"

_COPIED_STATUS = frozenset({"??", "!!"})
"""A copied file is untracked: listed as untracked, or as ignored when `.gitignore` names it."""

_HASH_MAX_BYTES = 1024 * 1024
"""The copier's own cap: a copied file is never larger, so a larger one is not a copy."""


class _Sessions(Protocol):
    async def get(self, session_id: SessionId) -> SessionRecord | None: ...


class _Rollovers(Protocol):
    async def open_rollovers(self) -> tuple[Rollover, ...]: ...


@dataclass(frozen=True, slots=True)
class CleanupReport:
    """What one pass did: the folders it removed, and why each other candidate stayed."""

    removed: tuple[Path, ...]
    kept: Mapping[Path, str]


@dataclass(frozen=True, slots=True)
class _Verdict:
    reason: str | None
    repo: Path | None = None
    entry: bool = False
    """Whether git holds a worktree entry for the folder."""


@contextlib.asynccontextmanager
async def _untracked() -> AsyncIterator[None]:
    yield


class WorktreeCleanupPass:
    """Asks every removal guard of each candidate folder, and removes the ones that pass."""

    def __init__(
        self,
        *,
        git: GitCheckout,
        store: WorktreeStore,
        sessions: _Sessions,
        rollovers: _Rollovers,
        repository: Callable[[ProjectId], Awaitable[Path | None]],
        operation: Callable[[], contextlib.AbstractAsyncContextManager[None]] = _untracked,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._git = git
        self._store = store
        self._sessions = sessions
        self._rollovers = rollovers
        self._repository = repository
        # `SessionLocks.operation` in `serve`, so shutdown waits for a removal under way.
        self._operation = operation
        self._clock = clock
        self._last_refusal: dict[Path, str] = {}
        self._claims_released = False

    async def pass_once(self) -> CleanupReport:
        """One pass over the candidates. A fault on one folder keeps that folder only.

        The first pass gives back every claim an earlier run left behind (a pass gives back
        its own on every exit path, so only a dead process leaves one); one host must run one
        pass. That write runs outside the operation gate; a pass that wakes after the drain
        shut the gate claims nothing and removes nothing."""
        if not self._claims_released:
            released = await self._store.release_stale_claims()
            self._claims_released = True
            if released:
                _LOG.info("released %d worktree removal claim(s) left by an earlier run", released)
        removed: list[Path] = []
        kept: dict[Path, str] = {}
        for worktree in await self._store.removable_candidates():
            fault: BaseException | None = None
            try:
                async with self._operation():
                    reason = await self._remove(worktree)
            except MutationsDraining:
                # Shutdown shut the gate before this folder was claimed: nothing was touched,
                # and the next start asks again.
                break
            except Exception as error:
                # Whatever failed, the folder stays and its claim was given back.
                fault, reason = error, _fault(error)
            if reason is None:
                removed.append(worktree.path)
                self._last_refusal.pop(worktree.path, None)
            else:
                kept[worktree.path] = reason
                self._refused(worktree.path, reason, fault)
        for path in set(self._last_refusal) - set(kept):
            # Gone from the candidates (resumed into, or removed): a return is news again.
            del self._last_refusal[path]
        return CleanupReport(removed=tuple(removed), kept=kept)

    async def kept(self) -> tuple[tuple[str, str, str], ...]:
        """Every recorded worktree not yet removed, as `(path, branch, reason)`, oldest first:
        what `doctor` lists. Read-only; a folder every guard lets go reads `removable`."""
        rows: dict[Path, Worktree] = {}
        for worktree in (await self._store.bound_folders()).values():
            if worktree.removed_at is None:
                rows.setdefault(worktree.path, worktree)
        listed = []
        for worktree in sorted(rows.values(), key=lambda w: (w.created_at, str(w.path))):
            try:
                reason = await self.verdict(worktree) or REMOVABLE
            except Exception as error:
                reason = _fault(error)
            listed.append((str(worktree.path), worktree.branch, reason))
        return tuple(listed)

    async def verdict(self, worktree: Worktree) -> str | None:
        """Why this folder must stay, or None when every guard lets it go.

        Read-only, and advisory outside a claim: a session may be bound to the folder the
        moment after it answers. Only the pass, holding the claim, acts on a None."""
        verdict = await self._judge(worktree)
        return verdict.reason

    async def _judge(self, worktree: Worktree) -> _Verdict:
        """The verdict, the main checkout it was reached through, and whether git holds an
        entry for the folder."""
        if worktree.removed_at is not None:
            return _Verdict(None)
        if worktree.base_branch is None:
            return _Verdict(DETACHED_BASE)
        bound = set(await self._store.bound_sessions(worktree.path))
        if not bound or await self._in_use(bound):
            return _Verdict(IN_USE)
        repo = await self._repository(worktree.project_id)
        if repo is None:
            return _Verdict(NOT_A_REPOSITORY)
        try:
            present = _present(worktree.path)
            if present and stat.S_ISLNK(os.lstat(worktree.path).st_mode):
                return _Verdict(DIRTY, repo)
            head = await self._git.worktree_head(repo, worktree.path)
            if head is None and present:
                return _Verdict("git: no worktree entry for the folder", repo)
            if head is None and not await self._git.has_branch(repo, worktree.branch):
                # Folder, git's entry and branch all gone: nothing of it is left to lose.
                return _Verdict(None, repo)
            if head is not None and head.branch != worktree.branch:
                return _Verdict(MOVED, repo, entry=True)
            merged = await self._git.is_ancestor(
                repo, f"refs/heads/{worktree.branch}", f"refs/heads/{worktree.base_branch}"
            )
            if not merged or await self._git.unmerged_history(
                repo, worktree.path, worktree.branch, worktree.base_branch
            ):
                return _Verdict(UNMERGED, repo, entry=head is not None)
            if present and not await self._clean(worktree):
                return _Verdict(DIRTY, repo, entry=True)
        except GitError as error:
            return _Verdict(f"git: {_one_line(error)}", repo)
        return _Verdict(None, repo, entry=head is not None)

    async def _in_use(self, bound: set[SessionId]) -> bool:
        """Whether a bound session is live, or its row is missing -- the store's own rule
        (`_HELD`), so `doctor` never calls removable a folder the pass would not list."""
        for session_id in bound:
            record = await self._sessions.get(session_id)
            if record is None or record.state not in TERMINAL_STATES:
                return True
        return any(
            rollover.predecessor_session_id in bound or rollover.successor_session_id in bound
            for rollover in await self._rollovers.open_rollovers()
        )

    async def _clean(self, worktree: Worktree) -> bool:
        """Whether the folder holds nothing but its copies, each unchanged."""
        for line in await self._git.status_lines(worktree.path):
            code, relative = line[:2], line[3:]
            recorded = worktree.copied.get(relative)
            if code not in _COPIED_STATUS or recorded is None:
                return False
            if _sha256(worktree.path, relative) != recorded:
                return False
        return True

    async def _remove(self, worktree: Worktree) -> str | None:
        """Guard, claim, guard again, remove. Answers the reason kept, or None once removed.

        Judged once before the claim, read-only: a kept folder -- most of them, most passes --
        is then never claimed and released, which would write the domain store twice a minute
        per folder and wake every surface's store watcher for nothing. The judgment is asked
        again under the claim, which is the one that decides."""
        early = await self._judge(worktree)
        if early.reason is not None:
            return early.reason
        if not await self._store.claim_for_removal(worktree.path, at=self._clock()):
            return IN_USE
        try:
            verdict = await self._judge(worktree)
            repo = verdict.repo
            if verdict.reason is not None or repo is None:
                await self._store.release_claim(worktree.path)
                return verdict.reason or NOT_A_REPOSITORY
            present = _present(worktree.path)
            try:
                await self._git.remove_worktree(repo, worktree.path)
            except GitError as error:
                if present or verdict.entry or _present(worktree.path):
                    # Kept: the folder is there, or git still holds its entry -- a locked one,
                    # say, for a folder on a drive not mounted now -- and with it the branch.
                    await self._store.release_claim(worktree.path)
                    return f"git: {_one_line(error)}"
                # Folder and entry both gone: git had nothing to remove.
        except BaseException:
            # The folder is still there (a cancel inside `worktree remove` may have taken part
            # of it, and the next pass then finds it dirty or its entry stale): give the claim
            # back now, not at the next pass's start, so a resume is not refused meanwhile.
            await self._store.release_claim(worktree.path)
            raise
        try:
            await self._delete_branch(repo, worktree)
        finally:
            # The folder is gone, so the row says so whatever became of the branch -- a cancel
            # included; a row left live would hold its claim until the next pass's start.
            await self._store.mark_removed(worktree.path, at=self._clock())
        with contextlib.suppress(OSError):
            # The per-project folder the worktree sat in, when nothing else is in it.
            worktree.path.parent.rmdir()
        _LOG.info("worktree %s and branch %s removed", worktree.path, worktree.branch)
        return None

    async def _delete_branch(self, repo: Path, worktree: Worktree) -> None:
        """`branch -d` the folder's branch; a refusal is logged with the command, never forced."""
        try:
            if not await self._git.has_branch(repo, worktree.branch):
                return
            await self._git.delete_merged_branch(repo, worktree.branch)
        except Exception as error:
            _LOG.warning(
                "worktree %s removed; its branch %s was kept (%s). It is merged into %s;"
                " delete it with `git -C %s branch -d %s` once git agrees",
                worktree.path,
                worktree.branch,
                _one_line(error),
                worktree.base_branch,
                repo,
                worktree.branch,
            )

    def _refused(self, path: Path, reason: str, fault: BaseException | None) -> None:
        """Log a kept folder once per change of its reason, not once per pass."""
        if self._last_refusal.get(path) == reason:
            return
        self._last_refusal[path] = reason
        if fault is None:
            _LOG.info("worktree %s kept: %s", path, reason)
        else:
            _LOG.error("worktree %s kept: %s", path, reason, exc_info=fault)


def _present(path: Path) -> bool:
    """Whether anything is at `path`. Only a missing path is absent: a folder that cannot be
    looked at raises, so it is kept rather than read as gone."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _sha256(tree: Path, relative: str) -> str | None:
    """A regular file's sha256, never read through a link; None for anything else."""
    current = tree
    parts = PurePosixPath(relative).parts
    for part in parts[:-1]:
        current = current / part
        try:
            if not stat.S_ISDIR(os.lstat(current).st_mode):
                return None
        except OSError:
            return None
    try:
        descriptor = os.open(tree.joinpath(*parts), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(_HASH_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    return None if len(data) > _HASH_MAX_BYTES else hashlib.sha256(data).hexdigest()


def _fault(error: Exception) -> str:
    """A kept reason for a fault: stable across passes, so it is logged once, not each time."""
    if isinstance(error, GitError):
        return f"git: {_one_line(error)}"
    return f"fault: {type(error).__name__}"


def _one_line(error: BaseException) -> str:
    text = str(error).strip()
    return (text.splitlines()[0] if text else type(error).__name__)[:200]
