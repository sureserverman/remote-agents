"""Every git argv the adapter builds, swept: reads carry their guards, nothing forces.

The sweep drives every `GitCheckout` method through a recording runner, so a method added to
the port without passing through here fails `test_the_sweep_covers_every_port_method` rather
than escaping the guard checks below.
"""

from __future__ import annotations

import inspect
import os
import stat
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from remote_agents.adapters.git.checkout import GitCli, GitResult
from remote_agents.ports.git_checkout import GitCheckout, GitError, GitTimeout

_READ_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
_WRITE_SHAPES = {
    ("worktree", "add", "-b"),
    ("worktree", "remove"),
    ("branch", "-d"),
}
_FORCING = {"--force", "-f", "-D", "-ff", "--force-with-lease"}


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, str], float]] = []

    async def run(
        self, argv: tuple[str, ...], *, env: Mapping[str, str], timeout: float
    ) -> GitResult:
        self.calls.append((argv, dict(env), timeout))
        if "--show-toplevel" in argv:
            return GitResult(0, b"/repo\n", b"")
        if "symbolic-ref" in argv:
            return GitResult(0, b"main\n", b"")
        if "rev-parse" in argv:
            return GitResult(0, b"a" * 40 + b"\n", b"")
        if "--numstat" in argv:
            return GitResult(0, b"3\t1\tf.txt\0", b"")
        if "rev-list" in argv:
            return GitResult(0, b"2\n", b"")
        return GitResult(0, b"", b"")


async def _sweep() -> tuple[_Recorder, set[str]]:
    recorder = _Recorder()
    cli = GitCli(recorder)
    repo, tree = Path("/repo"), Path("/state/worktrees/p/s")
    called = set()
    await cli.top_level(tree)
    called.add("top_level")
    await cli.head(repo)
    called.add("head")
    await cli.add_worktree(repo, tree, "ra/12345678", "a" * 40)
    called.add("add_worktree")
    await cli.remove_worktree(repo, tree)
    called.add("remove_worktree")
    await cli.delete_merged_branch(repo, "ra/12345678")
    called.add("delete_merged_branch")
    await cli.is_ancestor(repo, "ra/12345678", "main")
    called.add("is_ancestor")
    await cli.status_lines(tree)
    called.add("status_lines")
    await cli.diff_summary(tree, "a" * 40)
    called.add("diff_summary")
    return recorder, called


def _verb(argv: tuple[str, ...]) -> tuple[str, ...]:
    """The argv after `git`, its `-c` pairs and its `-C <dir>`."""
    rest = list(argv[1:])
    while rest and rest[0] in {"-c", "-C"}:
        rest = rest[2:]
    return tuple(rest)


def _is_write(argv: tuple[str, ...]) -> bool:
    verb = _verb(argv)
    return any(verb[: len(shape)] == shape for shape in _WRITE_SHAPES)


async def test_the_sweep_covers_every_port_method() -> None:
    _, called = await _sweep()
    declared = {
        name
        for name, member in inspect.getmembers(GitCheckout)
        if not name.startswith("_") and inspect.iscoroutinefunction(member)
    }
    assert called == declared


async def test_every_read_argv_carries_the_three_read_guards() -> None:
    recorder, _ = await _sweep()
    reads = [(argv, env) for argv, env, _ in recorder.calls if not _is_write(argv)]
    assert reads, "the sweep made no read call"
    for argv, env in reads:
        assert argv[:3] == ("git", "-c", "core.fsmonitor=false"), argv
        for key, value in _READ_ENV.items():
            assert env.get(key) == value, (argv, key)
        if "diff" in _verb(argv):
            assert "--no-ext-diff" in argv and "--no-textconv" in argv, argv


async def test_the_writes_are_exactly_the_three_shapes() -> None:
    recorder, _ = await _sweep()
    writes = {
        next(shape for shape in _WRITE_SHAPES if _verb(argv)[: len(shape)] == shape)
        for argv, _, _ in recorder.calls
        if _is_write(argv)
    }
    assert writes == _WRITE_SHAPES
    for argv, env, _ in recorder.calls:
        if _is_write(argv):
            assert env.get("GIT_TERMINAL_PROMPT") == "0", argv


async def test_no_argv_carries_a_forcing_flag_and_every_call_is_bounded() -> None:
    recorder, _ = await _sweep()
    for argv, _, timeout in recorder.calls:
        assert not _FORCING & set(argv), argv
        assert 0 < timeout <= 120, (argv, timeout)


async def test_a_ref_that_reads_as_an_option_is_refused_before_git_runs() -> None:
    recorder = _Recorder()
    cli = GitCli(recorder)
    with pytest.raises(GitError):
        await cli.is_ancestor(Path("/repo"), "--force", "main")
    with pytest.raises(GitError):
        await cli.delete_merged_branch(Path("/repo"), "-D")
    with pytest.raises(GitError):
        await cli.diff_summary(Path("/repo"), "--output=/tmp/x")
    assert recorder.calls == []


def _fake_git(directory: Path, body: str) -> None:
    script = directory / "git"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


async def test_a_hung_git_is_killed_at_its_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "pid"
    _fake_git(tmp_path, f'echo $$ > "{pid_file}"\nexec sleep 30\n')
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    cli = GitCli(read_timeout=0.5)
    started = time.monotonic()
    with pytest.raises(GitTimeout):
        await cli.head(tmp_path)
    assert time.monotonic() - started < 5
    pid = int(pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_output_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_git(tmp_path, "head -c 300000 /dev/zero | tr '\\0' 'x'\n")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    lines = await GitCli().status_lines(tmp_path)
    assert sum(len(line) for line in lines) <= 64 * 1024
