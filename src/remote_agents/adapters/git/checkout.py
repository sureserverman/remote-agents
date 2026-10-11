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
import hashlib
import os
import re
import signal
import stat
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
_READ_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
"""`LC_ALL=C`: git translates `diff --shortstat`'s sentence, and a German owner's git would
print `1 Datei geändert`, which no parser here reads. The C locale is also what makes gettext
ignore `LANGUAGE`."""
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
        status = await self._read(path, *_STATUS, "--ignored=traditional")
        if status.returncode != 0:
            raise GitRefused(_reason(status))
        # Past the cap -- a `.venv` or `node_modules` -- the folder is certainly more than its
        # copies: a closing `T?` entry says the rest went unread, rather than refusing the read.
        raw = status.stdout + (
            f"\0T? (more than {OUTPUT_CAP_BYTES} bytes of entries; the rest not read)".encode()
            if status.truncated
            else b""
        )
        # `assume-unchanged` and `skip-worktree` hide a tracked file's edit from `status`, and
        # `worktree remove` deletes that edit too. `ls-files -v` tags such a file with a
        # lowercase letter or `S`; each is listed as `<tag>? <path>`, edited or not, because
        # git will not say which.
        flagged = await self._read_ok(path, "ls-files", "-v", "-z", keep=_HIDDEN_TAGS)
        hidden = tuple(f"{entry[0]}? {entry[2:]}" for entry in _entries(flagged))
        # git skips an entry named `.git` it cannot open as a repository, so `status` never
        # names it; `worktree remove` deletes it anyway. Walked here, links never followed.
        try:
            nested = await asyncio.to_thread(_nested_git_entries, path)
        except OSError as error:
            raise GitRefused(f"a folder in {path} could not be read: {error}") from None
        return _entries(raw) + hidden + tuple(f"G? {entry}" for entry in nested)

    async def unmerged_history(self, repo: Path, path: Path, branch: str, base: str) -> int:
        tips: set[str] = set()
        admin = await self._admin_directory(repo, path)
        if admin is None and await self.worktree_head(repo, path) is not None:
            # git holds an entry this lookup cannot place: its refs and reflogs are unread, so
            # this is no answer -- never "nothing only it holds".
            raise GitRefused(f"git holds an entry for {path} but no admin folder matched it")
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
            tips.update(_reflog(head_log, exists=True))
            # And what its pseudo-refs name: with reflogs off, `ORIG_HEAD` alone holds a
            # commit `reset --hard` left behind.
            tips.update(await asyncio.to_thread(_pseudo_tips, admin))
        branch_log = await self._read(
            repo, "log", "-g", "--format=%H", _ref(f"refs/heads/{_ref(branch)}"), "--"
        )
        tips.update(_reflog(branch_log, exists=await self.has_branch(repo, branch)))
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

    async def merge_base(self, path: Path, one: str, other: str) -> str | None:
        result = await self._read(path, "merge-base", _ref(one), _ref(other), "--")
        if result.returncode == 1:
            return None
        if result.returncode != 0:
            raise GitRefused(_reason(result))
        return _text(result.stdout).strip() or None

    async def diff_summary(
        self, path: Path, base: str, copies: Mapping[str, str] | None = None
    ) -> GitSummary:
        base = _ref(base)
        # `--` ends the revisions: a base named like a top-level folder (`dev`, `docs`) is
        # otherwise ambiguous and git refuses the whole call. `--shortstat` is one line however
        # large the change, where a numstat of a few thousand files passed the output cap and
        # cost the line exactly when it had the most to say. A binary file is a changed file
        # with no lines in both. `--no-relative`: `diff.relative` would otherwise narrow the
        # counts to a subfolder while `status` below stays whole.
        shortstat = await self._read_ok(
            path, "diff", *_DIFF_GUARDS, "--no-renames", "--no-relative", "--shortstat", base, "--"
        )
        files, added, removed = _shortstat(_text(shortstat))
        ahead_raw = await self._read_ok(path, "rev-list", "--count", f"{base}..HEAD", "--")
        ahead_text = _text(ahead_raw).strip()
        head = await self.head(path)
        # Dirty in the summary's sense: anything `git status` reports without being asked
        # for ignored files, but an untouched copy the worktree was given. A status too long
        # to read is certainly not empty.
        status = await self._read(path, *_STATUS)
        if status.returncode != 0:
            raise GitRefused(_reason(status))
        given = copies or {}
        return GitSummary(
            branch=head.branch,
            files=files,
            added=added,
            removed=removed,
            ahead=int(ahead_text) if ahead_text.isdigit() else 0,
            dirty=status.truncated
            or await asyncio.to_thread(_dirty_beyond_copies, path, _entries(status.stdout), given),
        )


_SHORTSTAT = re.compile(
    r"(?P<files>\d+) files? changed"
    r"(?:, (?P<added>\d+) insertions?\(\+\))?"
    r"(?:, (?P<removed>\d+) deletions?\(-\))?"
)


def _shortstat(text: str) -> tuple[int, int, int]:
    """`files, added, removed` from `diff --shortstat`; an empty line is no change."""
    match = _SHORTSTAT.search(text)
    if match is None:
        if text.strip():
            raise GitRefused(f"git diff --shortstat answered {text.strip()[:60]!r}")
        return 0, 0, 0
    return (
        int(match["files"]),
        int(match["added"] or 0),
        int(match["removed"] or 0),
    )


_COPY_MAX_BYTES = 1024 * 1024
"""The copier's own cap (`application/worktrees.py`): a larger file is never a copy."""


def _dirty_beyond_copies(top: Path, entries: tuple[str, ...], copies: Mapping[str, str]) -> bool:
    """Whether any status entry is something other than an untouched copy. Reads files, so it
    runs on a worker thread: a hung mount stalls that thread, never the event loop."""
    return any(not _untouched_copy(top, entry, copies) for entry in entries)


def _untouched_copy(top: Path, entry: str, copies: Mapping[str, str]) -> bool:
    """Whether a status entry is an untracked file the worktree was given, unchanged: its
    recorded sha256 still matches, read without following a link. Spelled beside the cleanup
    pass's own check (`application/worktree_cleanup._sha256`), which this layer cannot import."""
    code, relative = entry[:2], entry[3:]
    recorded = copies.get(relative)
    if code != "??" or recorded is None:
        return False
    current = top
    parts = Path(relative).parts
    for part in parts[:-1]:
        current = current / part
        try:
            if not stat.S_ISDIR(os.lstat(current).st_mode):
                return False
        except OSError:
            return False
    try:
        descriptor = os.open(top.joinpath(*parts), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return False
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(_COPY_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    return len(data) <= _COPY_MAX_BYTES and hashlib.sha256(data).hexdigest() == recorded


_WORKTREE_REFS = ("refs/worktree", "refs/bisect", "refs/rewritten")
"""The refs git keeps per worktree; `worktree remove` deletes them with the worktree."""

_GITDIR_MAX_BYTES = 4096


def _reflog(result: GitResult, *, exists: bool) -> set[str]:
    """The commits a `log -g` listed. git exits 0 for a ref with no reflog, and skips an entry
    whose commit is gone (measured on git 2.43.0); it fails for a ref that does not exist.
    So a failure for a ref that `exists` is no answer, and neither is a reflog too long to
    read -- each raises, and the folder is kept."""
    if result.returncode != 0:
        if exists:
            raise GitRefused(_reason(result))
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
    """Which `<common>/worktrees/<name>` names `path` in its `gitdir` file. git records the
    real path, and may record it relative to the admin folder; both sides are resolved."""
    wanted = (path / ".git").resolve()
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
        named = Path(recorded.strip())
        if (named if named.is_absolute() else admin / named).resolve() == wanted:
            return admin
    return None


def _raise(error: OSError) -> None:
    raise error


def _nested_git_entries(top: Path) -> tuple[str, ...]:
    """Every entry named `.git` (any case) below `top`, relative, never through a link. The
    top's own `.git` file is the worktree's and is not listed."""
    found = []
    for folder, folders, files in os.walk(top, followlinks=False, onerror=_raise):
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
