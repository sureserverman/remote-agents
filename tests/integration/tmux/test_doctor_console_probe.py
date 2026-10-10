"""`doctor`'s console probe, against a real tmux server holding a renamed console (BL-043).

Run alone. It starts and kills its own server on a `remote-agents-test-` socket and never touches
`-L remote-agents`, where the owner's live agents are.
"""

from __future__ import annotations

import subprocess
import uuid

import pytest

from remote_agents.adapters.tmux.codec import CONSOLE_SESSION_NAME, CONSOLE_SLOT_OPTION
from remote_agents.bootstrap import _console_marks


@pytest.fixture
def server():
    socket = f"remote-agents-test-doctor-{uuid.uuid4().hex[:8]}"

    def tmux(*argv: str) -> str:
        return subprocess.run(
            ("tmux", "-L", socket, *argv), check=True, capture_output=True, text=True, timeout=10
        ).stdout

    try:
        yield socket, tmux
    finally:
        subprocess.run(("tmux", "-L", socket, "kill-server"), capture_output=True, timeout=10)


def _mark(tmux, target: str, slot: str) -> None:
    pane = tmux("display-message", "-p", "-t", target, "#{pane_id}").strip()
    tmux("set-option", "-p", "-t", pane, CONSOLE_SLOT_OPTION, slot)


def test_the_probe_reads_a_real_renamed_console(server) -> None:
    socket, tmux = server
    agent = f"ra-{uuid.uuid4()}"
    tmux("-f", "/dev/null", "new-session", "-d", "-s", CONSOLE_SESSION_NAME, "sleep 300")
    _mark(tmux, f"{CONSOLE_SESSION_NAME}:", "projects")
    tmux("new-session", "-d", "-s", agent, "sleep 300")
    _mark(tmux, f"{agent}:", "sessions")
    tmux("new-session", "-d", "-s", "scratch", "sleep 300")  # unmarked: never reported

    tmux("rename-session", "-t", CONSOLE_SESSION_NAME, "mine")

    assert sorted(_console_marks(socket)) == [
        ("mine", "projects", False),
        (agent, "sessions", True),
    ]


def test_a_host_with_no_server_has_no_marks() -> None:
    assert _console_marks(f"remote-agents-test-doctor-{uuid.uuid4().hex[:8]}") == ()


def test_a_read_that_fails_is_no_answer_rather_than_a_clean_console(monkeypatch) -> None:
    """A wedged server or a missing `tmux` must not read as "no renamed console"."""
    from remote_agents import bootstrap

    def times_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("tmux", 5)

    monkeypatch.setattr(bootstrap.subprocess, "run", times_out)
    assert _console_marks("remote-agents-test-doctor-wedged") is None

    def refuses(*_args, **_kwargs):
        return subprocess.CompletedProcess(("tmux",), 1, "", "server exited unexpectedly")

    monkeypatch.setattr(bootstrap.subprocess, "run", refuses)
    assert _console_marks("remote-agents-test-doctor-refusing") is None
