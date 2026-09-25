"""A scratch console wearing the function-key bar, shown by a real tmux client.

Only a real client has a `client_width`, and only its terminal shows the status row, so the
client runs `tmux attach` inside a pane of an outer scratch server sized to the width under
test, and that pane is what gets captured -- as `tests/integration/tmux/test_status_bar_probe.py`
first measured.
"""

from __future__ import annotations

import subprocess
import time
from uuid import uuid4

from remote_agents.adapters.tmux.codec import CONSOLE_SESSION_NAME, status_format_args
from remote_agents.adapters.tui.keys import status_bar_keys
from remote_agents.adapters.tui.theme import THEMES, status_bar_palette

NIGHT = status_bar_palette(THEMES[0])


def tmux(socket: str, *args: str) -> str:
    return subprocess.run(
        ["tmux", "-L", socket, *args], capture_output=True, text=True, check=True
    ).stdout


def wait_for(ready, bound: float = 5.0) -> bool:
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        if ready():
            return True
        time.sleep(0.02)
    return False


class BarConsole:
    """A scratch `ra-console` session wearing the bar, shown by a client in an outer pane."""

    def __init__(self, width: int) -> None:
        tag = uuid4().hex
        self.inner = f"remote-agents-bar-in-{tag}"
        self.outer = f"remote-agents-bar-out-{tag}"
        self.width = width

    def tmux(self, *args: str) -> str:
        return tmux(self.inner, *args)

    def start(self, palette=NIGHT) -> None:
        self.tmux(
            "new-session", "-d", "-s", CONSOLE_SESSION_NAME, "-x", "80", "-y", "24", "sleep 600"
        )
        for argv in status_format_args(status_bar_keys(), palette):
            self.tmux(*argv)
        attach = f"env -u TMUX tmux -L {self.inner} attach-session -t {CONSOLE_SESSION_NAME}:"
        tmux(self.outer, "new-session", "-d", "-s", "o", "-x", str(self.width), "-y", "10", attach)
        assert wait_for(lambda: self.status_row().strip() != ""), "the client never drew"

    def run(self, argv: tuple[str, ...]) -> None:
        """Issue one codec-built argv suffix against this console's server."""
        self.tmux(*argv)

    def set(self, name: str, value: str) -> None:
        self.tmux("set-option", "-w", "-t", f"{CONSOLE_SESSION_NAME}:", name, value)

    def status_row(self) -> str:
        screen = tmux(self.outer, "capture-pane", "-p", "-t", "o:")
        return screen.rstrip("\n").split("\n")[-1]

    def settled_row(self, wanted) -> str:
        wait_for(lambda: wanted(self.status_row()))
        return self.status_row()

    def expanded(self, status_format: str) -> str:
        """*status_format* as this client expands it, styles still in."""
        client = self.tmux("list-clients", "-F", "#{client_name}").split()[0]
        return self.tmux(
            "display-message", "-p", "-c", client, "-t", f"{CONSOLE_SESSION_NAME}:", status_format
        )

    def close(self) -> None:
        for socket in (self.outer, self.inner):
            subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, check=False)
