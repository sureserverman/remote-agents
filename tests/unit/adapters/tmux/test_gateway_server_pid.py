"""`server_pid` reads the dedicated server's process id for `doctor` (BL-147)."""

from __future__ import annotations

import asyncio

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway


class _Runner:
    def __init__(self, output: str = "", error: RuntimeError | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        if self.error is not None:
            raise self.error
        return self.output


def _pid(runner: _Runner) -> int | None:
    return asyncio.run(TmuxGateway("remote-agents-test-pid", runner).server_pid())


def test_the_running_server_reports_its_pid() -> None:
    runner = _Runner("7998\n")

    assert _pid(runner) == 7998
    assert runner.calls == [
        ("tmux", "-L", "remote-agents-test-pid", "display-message", "-p", "#{pid}")
    ]


def test_no_server_is_none() -> None:
    error = RuntimeError("no server running on /tmp/tmux-1000/remote-agents-test-pid")
    assert _pid(_Runner(error=error)) is None


def test_garbage_is_none() -> None:
    assert _pid(_Runner("")) is None


def test_any_other_failure_raises() -> None:
    with pytest.raises(RuntimeError):
        _pid(_Runner(error=RuntimeError("tmux: permission denied")))
