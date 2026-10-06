"""The background-paint hooks, against a real tmux server and a real client terminal.

The unit tests pin the hook string; only tmux can say what it does with it. Measured on the
live console at v0.59.0: a client whose terminal closed under it left the server, the global
`client-detached` hook wrote to a tty that no longer existed, and tmux showed the failure in
view mode over the active pane -- the projects pane, which F12 then brought back still covered.

Run alone. It starts and kills its own server on a `remote-agents-test-` socket and never
touches `-L remote-agents`, where the owner's live agents are.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import pytest

from remote_agents.adapters.tmux.codec import console_background_hook_args, console_target

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="a vanished client tty is measured on Linux"
)


def _tmux(socket: str, *argv: str) -> str:
    return subprocess.run(
        ("tmux", "-L", socket, *argv), check=True, capture_output=True, text=True
    ).stdout


def _wait(predicate, budget: float = 5.0) -> bool:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@pytest.fixture
def console():
    """A console session on a throwaway server, carrying the hooks the real console carries."""
    socket = f"remote-agents-test-paint-{uuid.uuid4().hex[:8]}"
    session = console_target().rstrip(":")
    _tmux(socket, "new-session", "-d", "-s", session, "-x", "80", "-y", "20", "sleep 300")
    for argv in console_background_hook_args("#0F1115"):
        _tmux(socket, *argv)
    try:
        yield socket
    finally:
        subprocess.run(("tmux", "-L", socket, "kill-server"), capture_output=True)


def test_a_client_whose_terminal_vanished_leaves_no_notice_over_a_pane(console) -> None:
    socket = console
    primary, secondary = os.openpty()
    # Without `TMUX`: a suite run from inside tmux would otherwise refuse to nest the attach.
    environment = {k: v for k, v in os.environ.items() if k != "TMUX"}
    client = subprocess.Popen(
        ("tmux", "-L", socket, "attach-session", "-t", console_target()),
        stdin=secondary, stdout=secondary, stderr=secondary,
        start_new_session=True, env={**environment, "TERM": "xterm-256color"},
    )  # fmt: skip
    os.close(secondary)
    try:
        assert _wait(lambda: _tmux(socket, "list-clients", "-F", "#{client_name}").strip())
        tty = _tmux(socket, "list-clients", "-F", "#{client_name}").strip()
        # The terminal goes first, then the client with no chance to detach cleanly: the
        # order a closed terminal window produces, and the one that left the tty gone by
        # the time `client-detached` ran.
        os.close(primary)
        primary = None
        client.kill()
        client.wait()
        assert _wait(lambda: not os.path.exists(tty)), tty
        assert _wait(lambda: not _tmux(socket, "list-clients").strip())
        # The hook is `run-shell -b`; give it the time a notice would take to appear.
        time.sleep(0.5)
        assert _tmux(socket, "list-panes", "-a", "-F", "#{pane_in_mode}").split() == ["0"]
    finally:
        if primary is not None:
            os.close(primary)
        if client.poll() is None:
            client.kill()
            client.wait()
