"""`GitCli`: the one place remote-agents runs `git`, bounded and guarded.

**Reads change nothing in the repository.** `git status` refreshes the index and takes
`index.lock` to do it, which collides with an agent committing in the same checkout at that
moment; `GIT_OPTIONAL_LOCKS=0` stops it. `core.fsmonitor` and diff drivers run programs the
repository's own config names, so a read turns them off: `-c core.fsmonitor=false` on every
read, `--no-ext-diff --no-textconv` on every diff. `GIT_TERMINAL_PROMPT=0` on every call, so a
credential helper never waits on a terminal nobody is watching.

**Writes are exactly three shapes:** `worktree add -b`, `worktree remove` and `branch -d`. No
forcing flag is spelled anywhere in this package (`tests/contract/adapters/git/test_git_argv.py`
sweeps every argv). A refusal comes back as `GitRefused` and is left standing.

**Every call is bounded.** A call that outlives its timeout is killed and reaped and raises
`GitTimeout`. Output is read up to `OUTPUT_CAP_BYTES` and the rest is drained unread, so a
repository with a huge status costs bounded memory.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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


class GitRunner(Protocol):
    """Argument-vector subprocess boundary, bounded by an explicit timeout."""

    async def run(
        self, argv: tuple[str, ...], *, env: Mapping[str, str], timeout: float
    ) -> GitResult: ...


class ProcessGitRunner:
    """Run one git argv without a shell, `stdin` closed, killed at its timeout."""

    async def run(
        self, argv: tuple[str, ...], *, env: Mapping[str, str], timeout: float
    ) -> GitResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **env},
            )
        except FileNotFoundError:
            raise GitError("git is not installed on this host") from None
        assert process.stdout is not None and process.stderr is not None
        try:
            stdout, stderr, _ = await asyncio.wait_for(
                asyncio.gather(
                    _read_capped(process.stdout, OUTPUT_CAP_BYTES),
                    _read_capped(process.stderr, _STDERR_CAP_BYTES),
                    process.wait(),
                ),
                timeout=timeout,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise GitTimeout(f"git {_verb_of(argv)} did not finish in {timeout:g}s") from None
        return GitResult(process.returncode or 0, stdout, stderr)


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> bytes:
    """Keep the first `cap` bytes and drain the rest, so the child never blocks on a full pipe."""
    kept = bytearray()
    while chunk := await stream.read(65536):
        if len(kept) < cap:
            kept += chunk[: cap - len(kept)]
    return bytes(kept)


def _verb_of(argv: tuple[str, ...]) -> str:
    rest = list(argv[1:])
    while rest and rest[0] in {"-c", "-C"}:
        rest = rest[2:]
    return " ".join(rest[:2])


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

    async def _read(self, path: Path, *args: str) -> GitResult:
        argv = (*_READ_PREFIX, "-C", str(path), *args)
        return await self._runner.run(argv, env=_READ_ENV, timeout=self._read_timeout)

    async def _read_ok(self, path: Path, *args: str) -> bytes:
        result = await self._read(path, *args)
        if result.returncode != 0:
            raise GitRefused(_reason(result))
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
        branch = await self._read(path, "symbolic-ref", "--quiet", "--short", "HEAD")
        name = _text(branch.stdout).strip() if branch.returncode == 0 else ""
        return GitHead(commit=commit, branch=name or None)

    async def add_worktree(self, repo: Path, path: Path, branch: str, commit: str) -> None:
        await self._write(repo, "worktree", "add", "-b", _ref(branch), str(path), _ref(commit))

    async def remove_worktree(self, repo: Path, path: Path) -> None:
        await self._write(repo, "worktree", "remove", str(path))

    async def delete_merged_branch(self, repo: Path, branch: str) -> None:
        await self._write(repo, "branch", "-d", _ref(branch))

    async def is_ancestor(self, repo: Path, commit: str, ref: str) -> bool:
        result = await self._read(repo, "merge-base", "--is-ancestor", _ref(commit), _ref(ref))
        if result.returncode in (0, 1):
            return result.returncode == 0
        raise GitRefused(_reason(result))

    async def status_lines(self, path: Path) -> tuple[str, ...]:
        raw = await self._read_ok(
            path, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"
        )
        return tuple(_text(entry) for entry in raw.split(b"\0") if entry)

    async def diff_summary(self, path: Path, base: str) -> GitSummary:
        base = _ref(base)
        numstat = await self._read_ok(path, "diff", *_DIFF_GUARDS, "--numstat", "-z", base)
        files = added = removed = 0
        for entry in numstat.split(b"\0"):
            fields = entry.split(b"\t")
            if len(fields) < 2:
                continue
            files += 1
            # A binary file reports `-` for both counts; it is a changed file with no lines.
            added += int(fields[0]) if fields[0].isdigit() else 0
            removed += int(fields[1]) if fields[1].isdigit() else 0
        ahead_raw = await self._read_ok(path, "rev-list", "--count", f"{base}..HEAD")
        ahead_text = _text(ahead_raw).strip()
        head = await self.head(path)
        return GitSummary(
            branch=head.branch,
            files=files,
            added=added,
            removed=removed,
            ahead=int(ahead_text) if ahead_text.isdigit() else 0,
            dirty=bool(await self.status_lines(path)),
        )
