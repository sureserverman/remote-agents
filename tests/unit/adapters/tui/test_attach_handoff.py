"""Unit tests for the exit-and-attach handoff that ends a local launch."""

from __future__ import annotations

import pytest

from remote_agents.adapters.tui.app import AttachRequest
from remote_agents.adapters.tui.attach import attach_to

_ARGV = ("tmux", "-L", "remote-agents", "attach-session", "-t", "=ra-abc")


class Recorder:
    """Stand in for the exec that would otherwise replace this process."""

    def __init__(self, error: OSError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def __call__(self, file: str, argv: tuple[str, ...]) -> None:
        self.calls.append((file, argv))
        if self.error is not None:
            raise self.error


def _request() -> AttachRequest:
    return AttachRequest("abc", _ARGV)


def test_a_ready_launch_execs_exactly_the_attach_argument_vector() -> None:
    exec_argv = Recorder()
    reported: list[str] = []

    status = attach_to(_request(), environment={}, exec_argv=exec_argv, report=reported.append)

    assert status == 0
    assert exec_argv.calls == [("tmux", _ARGV)]
    assert reported == []


def test_nothing_is_exec_ed_when_the_owner_quit_without_launching() -> None:
    exec_argv = Recorder()

    status = attach_to(None, environment={}, exec_argv=exec_argv, report=lambda _: None)

    assert status == 0
    assert exec_argv.calls == []


def test_an_existing_tmux_client_is_told_how_to_switch_rather_than_nested() -> None:
    """Attaching inside a client nests one tmux in another; refuse and hand over the command."""
    exec_argv = Recorder()
    reported: list[str] = []

    status = attach_to(
        _request(),
        environment={"TMUX": "/tmp/tmux-1000/default,123,0"},
        exec_argv=exec_argv,
        report=reported.append,
    )

    assert status == 0
    assert exec_argv.calls == []
    assert "Already inside tmux" in reported[0]
    assert "attach-session" in reported[0]


def test_an_exec_that_cannot_run_leaves_the_owner_a_command() -> None:
    exec_argv = Recorder(error=OSError("tmux is not on PATH"))
    reported: list[str] = []

    status = attach_to(_request(), environment={}, exec_argv=exec_argv, report=reported.append)

    assert status == 1
    assert "Could not attach automatically" in reported[0]
    assert "attach-session" in reported[0]


def test_the_attach_argv_is_a_vector_never_a_shell_string() -> None:
    request = _request()

    assert request.argv[0] == "tmux"
    assert all(isinstance(argument, str) for argument in request.argv)
    assert not any(character in " ".join(request.argv[:-1]) for character in ";|&$`")


def test_an_attach_request_without_a_command_is_refused() -> None:
    with pytest.raises(ValueError):
        AttachRequest("abc", ())
    with pytest.raises(ValueError):
        AttachRequest("", _ARGV)
