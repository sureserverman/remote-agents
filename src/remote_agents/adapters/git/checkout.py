"""`GitCli`: the one place remote-agents runs `git`, bounded and guarded.

**Reads change nothing in the repository.** `git status` refreshes the index and takes
`index.lock` to do it, which collides with an agent committing in the same checkout at that
moment; `GIT_OPTIONAL_LOCKS=0` stops it. `core.fsmonitor` and diff drivers run programs the
repository's own config names, so a read turns those off: `-c core.fsmonitor=false` on every
read, `--no-ext-diff --no-textconv` on every diff. Clean/smudge filter drivers are not turned
off -- they have no global switch -- which is one more reason every call is bounded below.
`GIT_TERMINAL_PROMPT=0` on every call, so a credential helper never waits on a terminal
nobody is watching.

**git acts only on the repository it is pointed at.** Every inherited `GIT_*` variable is
dropped before git starts: `GIT_DIR`, `GIT_WORK_TREE` and `GIT_INDEX_FILE` override `-C`, and
`GIT_CONFIG_PARAMETERS` injects config, so a process started from a git hook or a dotfile
shell would otherwise aim `worktree add` and `branch -d` at another repository.

**Writes are exactly three shapes:** `worktree add -b`, `worktree remove` and `branch -d`. No
forcing flag is spelled anywhere in this package (`tests/contract/adapters/git/test_git_argv.py`
sweeps every argv). A refusal comes back as `GitRefused` and is left standing.

**Every call is bounded, and so is everything it starts.** git runs in its own process group
and writes into unlinked temporary files, not pipes. A hook, filter or fsmonitor that git
starts inherits those, and with pipes it held them open: asyncio's `wait()` does not return
until every holder closes them, so a backgrounded hook made a finished `worktree add` read as
a timeout, and a hung one made the bound no bound at all. With files, the call ends when git
does. git's process group is then killed -- on success, timeout or cancellation alike -- so
nothing git started keeps writing into a worktree after it returns, short of a hook that
deliberately left the group (`setsid`), which no group kill can reach.

**Output is capped, and a capped answer is not an answer.** At most `OUTPUT_CAP_BYTES` is
read. A read whose output was longer raises `GitRefused` rather than return a list missing its
tail: a status cut short reads as fewer changes than the folder holds.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

from remote_agents.ports.git_checkout import (
    GitError,
    GitHead,
    GitRefused,
    GitSummary,
    GitTimeout,
)

OUTPUT_CAP_BYTES = 64 * 1024
_STDERR_CAP_BYTES = 4 * 1024
_READ_TIMEOUT_SECONDS = 5.0
_WRITE_TIMEOUT_SECONDS = 60.0
"""`worktree add` checks out the whole tree; a large repository takes seconds, not millis."""
_REAP_SECONDS = 5.0
"""How long to wait for git to be reaped after its group was sent SIGKILL."""

_READ_PREFIX = ("git", "-c", "core.fsmonitor=false")
_READ_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
_WRITE_ENV = {"GIT_TERMINAL_PROMPT": "0"}
_DIFF_GUARDS = ("--no-ext-diff", "--no-textconv")


@dataclass(frozen=True, slots=True)
class GitResult:
    """One finished git call: its exit status and its capped output."""

    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool = False
    """Whether stdout was longer than `OUTPUT_CAP_BYTES` and was cut."""


class GitRunner(Protocol):
    """Argument-vector subprocess boundary, bounded by an explicit timeout.

    `keep`, when given, is the set of first bytes a NUL-separated stdout entry must start
    with to be kept: only kept entries are returned, and only they count toward the cap.
    """

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str],
        timeout: float,
        keep: frozenset[int] | None = None,
    ) -> GitResult: ...


class ProcessGitRunner:
    """Run one git argv without a shell, `stdin` closed, in its own process group."""

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str],
        timeout: float,
        keep: frozenset[int] | None = None,
    ) -> GitResult:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    env=child_environment(env),
                    start_new_session=True,
                )
            except FileNotFoundError:
                raise GitError("git is not installed on this host") from None
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except TimeoutError:
                raise GitTimeout(f"git {_verb_of(argv)} did not finish in {timeout:g}s") from None
            finally:
                # On success too: a hook git backgrounded is still in the group, and it would
                # go on writing into the worktree after this call reported it finished.
                _kill_group(process.pid)
                if process.returncode is None:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(process.wait(), timeout=_REAP_SECONDS)
            if keep is None:
                out, truncated = _read_back(stdout, OUTPUT_CAP_BYTES)
            else:
                out, truncated = _read_entries(stdout, keep, OUTPUT_CAP_BYTES)
            err, _ = _read_back(stderr, _STDERR_CAP_BYTES)
        return GitResult(process.returncode or 0, out, err, truncated)


def child_environment(extra: Mapping[str, str]) -> dict[str, str]:
    """This process's environment without any `GIT_*` variable, plus `extra`."""
    kept = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return {**kept, **extra}


def _kill_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def _read_back(file: IO[bytes], cap: int) -> tuple[bytes, bool]:
    file.seek(0)
    data = file.read(cap + 1)
    return data[:cap], len(data) > cap


def _read_entries(file: IO[bytes], keep: frozenset[int], cap: int) -> tuple[bytes, bool]:
    """The NUL-separated entries starting with a byte in `keep`, read a chunk at a time."""
    file.seek(0)
    kept = bytearray()
    pending = b""
    while chunk := file.read(65536):
        *whole, pending = (pending + chunk).split(b"\0")
        for entry in whole:
            if entry and entry[0] in keep:
                if len(kept) + len(entry) + 1 > cap:
                    return bytes(kept), True
                kept += entry + b"\0"
    if pending and pending[0] in keep:
        kept += pending
    return bytes(kept), False


def _verb_of(argv: tuple[str, ...]) -> str:
    rest = list(argv[1:])
    while rest and rest[0] in {"-c", "-C"}:
        rest = rest[2:]
    return " ".join(rest[:2])


def _absolute(path: Path) -> str:
    """A worktree path is always absolute here, so it can never read as an option."""
    if not path.is_absolute():
        raise GitError(f"refusing a relative worktree path: {str(path)[:80]!r}")
    return str(path)


def _ref(value: str) -> str:
    """Refuse a ref git would read as an option. Refs here are ours or git's own output."""
    if not value or value.startswith("-"):
        raise GitError(f"refusing a ref that reads as an option: {value[:40]!r}")
    return value


def _reason(result: GitResult) -> str:
    text = result.stderr.decode("utf-8", errors="replace").strip()
    first = text.splitlines()[0] if text else f"git exited {result.returncode}"
    return first[:200]


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


class GitCli:
    """`GitCheckout` over the `git` executable on PATH."""

    def __init__(
        self,
        runner: GitRunner | None = None,
        *,
        read_timeout: float = _READ_TIMEOUT_SECONDS,
        write_timeout: float = _WRITE_TIMEOUT_SECONDS,
    ) -> None:
        self._runner = runner or ProcessGitRunner()
        self._read_timeout = read_timeout
        self._write_timeout = write_timeout

    async def _read(self, path: Path, *args: str, keep: frozenset[int] | None = None) -> GitResult:
        argv = (*_READ_PREFIX, "-C", str(path), *args)
        if keep is None:
            return await self._runner.run(argv, env=_READ_ENV, timeout=self._read_timeout)
        return await self._runner.run(argv, env=_READ_ENV, timeout=self._read_timeout, keep=keep)

    async def _read_ok(self, path: Path, *args: str, keep: frozenset[int] | None = None) -> bytes:
        result = await self._read(path, *args, keep=keep)
        if result.returncode != 0:
            raise GitRefused(_reason(result))
        if result.truncated:
            raise GitRefused(f"git {args[0]} wrote more than {OUTPUT_CAP_BYTES} bytes; not read")
        return result.stdout

    async def _write(self, repo: Path, *args: str) -> None:
        argv = ("git", "-C", str(repo), *args)
        result = await self._runner.run(argv, env=_WRITE_ENV, timeout=self._write_timeout)
        if result.returncode != 0:
            raise GitRefused(_reason(result))

    async def top_level(self, path: Path) -> Path | None:
        result = await self._read(path, "rev-parse", "--show-toplevel")
        if result.returncode != 0:
            return None
        line = _text(result.stdout).strip()
        return Path(line) if line else None

    async def head(self, path: Path) -> GitHead:
        commit = _text(await self._read_ok(path, "rev-parse", "--verify", "HEAD")).strip()
        # The full ref, not `--short`: a tag sharing the branch's name makes `--short` answer
        # `heads/main`, which is no branch name at all.
        branch = await self._read(path, "symbolic-ref", "--quiet", "HEAD")
        ref = _text(branch.stdout).strip() if branch.returncode == 0 else ""
        name = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ""
        return GitHead(commit=commit, branch=name or None)

    async def worktree_head(self, repo: Path, path: Path) -> GitHead | None:
        raw = await self._read_ok(repo, "worktree", "list", "--porcelain", "-z")
        wanted = Path(_absolute(path)).resolve()
        # Records are NUL-separated attribute lines, each record ended by an empty one.
        record: dict[str, str] = {}
        for line in (*_text(raw).split("\0"), ""):
            if line:
                key, _, value = line.partition(" ")
                record.setdefault(key, value)
                continue
            if "worktree" in record and Path(record["worktree"]).resolve() == wanted:
                ref = record.get("branch", "")
                name = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ""
                return GitHead(commit=record.get("HEAD", ""), branch=name or None)
            record = {}
        return None

    async def has_branch(self, repo: Path, branch: str) -> bool:
        result = await self._read(
            repo, "rev-parse", "--verify", "--quiet", _ref(f"refs/heads/{_ref(branch)}")
        )
        if result.returncode in (0, 1):
            return result.returncode == 0
        raise GitRefused(_reason(result))

    async def add_worktree(self, repo: Path, path: Path, branch: str, commit: str) -> None:
        await self._write(
            repo, "worktree", "add", "-b", _ref(branch), _absolute(path), _ref(commit)
        )

    async def remove_worktree(self, repo: Path, path: Path) -> None:
        await self._write(repo, "worktree", "remove", _absolute(path))

    async def delete_merged_branch(self, repo: Path, branch: str) -> None:
        await self._write(repo, "branch", "-d", _ref(branch))

    async def is_ancestor(self, repo: Path, commit: str, ref: str) -> bool:
        result = await self._read(repo, "merge-base", "--is-ancestor", _ref(commit), _ref(ref))
        if result.returncode in (0, 1):
            return result.returncode == 0
        raise GitRefused(_reason(result))

    async def status_lines(self, path: Path) -> tuple[str, ...]:
        # `--ignored=traditional` with `--untracked-files=all` lists every ignored *file*, one
        # entry each: `worktree remove` refuses modified and untracked files but deletes ignored
        # ones without asking, and the files a worktree copies in are usually ignored ones.
        raw = await self._read_ok(path, *_STATUS, "--ignored=traditional")
        # `assume-unchanged` and `skip-worktree` hide a tracked file's edit from `status`, and
        # `worktree remove` deletes that edit too. `ls-files -v` tags such a file with a
        # lowercase letter or `S`; each is listed as `<tag>? <path>`, edited or not, because
        # git will not say which.
        flagged = await self._read_ok(path, "ls-files", "-v", "-z", keep=_HIDDEN_TAGS)
        hidden = tuple(f"{entry[0]}? {entry[2:]}" for entry in _entries(flagged))
        # git skips an entry named `.git` it cannot open as a repository, so `status` never
        # names it; `worktree remove` deletes it anyway. Walked here, links never followed.
        nested = await asyncio.to_thread(_nested_git_entries, path)
        return _entries(raw) + hidden + tuple(f"G? {entry}" for entry in nested)

    async def unmerged_history(self, repo: Path, path: Path, branch: str, base: str) -> int:
        tips: set[str] = set()
        admin = await self._admin_directory(repo, path)
        if admin is not None:
            # As that worktree sees itself: its own refs, and every commit its HEAD has been.
            git_dir = f"--git-dir={admin}"
            refs = await self._read_ok(
                repo,
                git_dir,
                "for-each-ref",
                "--format=%(objectname)",
                *_WORKTREE_REFS,
            )
            tips.update(_text(refs).split())
            head_log = await self._read(repo, git_dir, "log", "-g", "--format=%H", "HEAD", "--")
            tips.update(_reflog(head_log))
            # And what its pseudo-refs name: with reflogs off, `ORIG_HEAD` alone holds a
            # commit `reset --hard` left behind.
            tips.update(await asyncio.to_thread(_pseudo_tips, admin))
        branch_log = await self._read(
            repo, "log", "-g", "--format=%H", _ref(f"refs/heads/{_ref(branch)}"), "--"
        )
        tips.update(_reflog(branch_log))
        if not tips:
            return 0
        counted = await self._read_ok(
            repo,
            "rev-list",
            "--count",
            *sorted(tips),
            "--not",
            _ref(f"refs/heads/{_ref(base)}"),
            "--",
        )
        text = _text(counted).strip()
        if not text.isdigit():
            raise GitRefused(f"git rev-list answered {text[:40]!r}, not a count")
        return int(text)

    async def _admin_directory(self, repo: Path, path: Path) -> Path | None:
        """The folder git keeps the worktree at `path` under, or None when it holds none."""
        common = _text(
            await self._read_ok(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).strip()
        return await asyncio.to_thread(_admin_for, Path(common), Path(_absolute(path)))

    async def diff_summary(self, path: Path, base: str) -> GitSummary:
        base = _ref(base)
        # `--` ends the revisions: a base named like a top-level folder (`dev`, `docs`) is
        # otherwise ambiguous and git refuses the whole call.
        numstat = await self._read_ok(
            path, "diff", *_DIFF_GUARDS, "--no-renames", "--numstat", "-z", base, "--"
        )
        files = added = removed = 0
        for entry in numstat.split(b"\0"):
            fields = entry.split(b"\t")
            if len(fields) < 2:
                continue
            files += 1
            # A binary file reports `-` for both counts; it is a changed file with no lines.
            added += int(fields[0]) if fields[0].isdigit() else 0
            removed += int(fields[1]) if fields[1].isdigit() else 0
        ahead_raw = await self._read_ok(path, "rev-list", "--count", f"{base}..HEAD", "--")
        ahead_text = _text(ahead_raw).strip()
        head = await self.head(path)
        # Dirty in the summary's sense: anything `git status` reports without being asked
        # for ignored files. A status too long to read is certainly not empty.
        status = await self._read(path, *_STATUS)
        if status.returncode != 0:
            raise GitRefused(_reason(status))
        return GitSummary(
            branch=head.branch,
            files=files,
            added=added,
            removed=removed,
            ahead=int(ahead_text) if ahead_text.isdigit() else 0,
            dirty=status.truncated or bool(_entries(status.stdout)),
        )


_WORKTREE_REFS = ("refs/worktree", "refs/bisect", "refs/rewritten")
"""The refs git keeps per worktree; `worktree remove` deletes them with the worktree."""

_GITDIR_MAX_BYTES = 4096


def _reflog(result: GitResult) -> set[str]:
    """The commits a `log -g` listed. A ref with no reflog, or no ref at all, lists none; a
    reflog too long to read is not an answer."""
    if result.returncode != 0:
        return set()
    if result.truncated:
        raise GitRefused(f"a reflog longer than {OUTPUT_CAP_BYTES} bytes; not read")
    return set(_text(result.stdout).split())


_PSEUDO_REFS = (
    "ORIG_HEAD",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "REBASE_HEAD",
    "BISECT_HEAD",
    "AUTO_MERGE",
    "rebase-merge/orig-head",
    "rebase-apply/orig-head",
)
"""Per-worktree files naming a commit, kept in the worktree's admin folder and deleted with it."""

_OBJECT_ID = frozenset("0123456789abcdef")


def _pseudo_tips(admin: Path) -> set[str]:
    """The object ids the worktree's pseudo-refs name; a missing or odd file names none."""
    tips = set()
    for name in _PSEUDO_REFS:
        try:
            with open(admin / name, "rb") as handle:
                text = handle.read(_GITDIR_MAX_BYTES).decode("ascii", "replace")
        except OSError:
            continue
        words = text.split()
        if words and len(words[0]) in (40, 64) and set(words[0]) <= _OBJECT_ID:
            tips.add(words[0])
    return tips


def _admin_for(common: Path, path: Path) -> Path | None:
    """Which `<common>/worktrees/<name>` names `path` in its `gitdir` file."""
    wanted = os.path.normpath(path / ".git")
    try:
        candidates = sorted((common / "worktrees").iterdir())
    except FileNotFoundError:
        return None
    for admin in candidates:
        try:
            with open(admin / "gitdir", "rb") as handle:
                recorded = handle.read(_GITDIR_MAX_BYTES).decode("utf-8", "surrogateescape")
        except OSError:
            continue
        if os.path.normpath(recorded.strip()) == wanted:
            return admin
    return None


def _nested_git_entries(top: Path) -> tuple[str, ...]:
    """Every entry named `.git` (any case) below `top`, relative, never through a link. The
    top's own `.git` file is the worktree's and is not listed."""
    found = []
    for folder, folders, files in os.walk(top, followlinks=False):
        relative = Path(folder).relative_to(top)
        for name in (*folders, *files):
            if name.lower() == ".git" and (relative != Path(".") or name != ".git"):
                found.append((relative / name).as_posix())
        # Never descend into a nested `.git` folder: it is listed, which is enough to keep.
        folders[:] = [name for name in folders if name.lower() != ".git"]
    return tuple(sorted(found))


_HIDDEN_TAGS = frozenset(b"abcdefghijklmnopqrstuvwxyzS")
"""`ls-files -v` tags for a tracked file whose changes git will not report."""

_STATUS = ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")


def _entries(raw: bytes) -> tuple[str, ...]:
    return tuple(_text(entry) for entry in raw.split(b"\0") if entry)
