"""Real dedicated-socket capture is passed through the safety boundary."""

import subprocess
from pathlib import Path
from uuid import uuid4

from remote_agents.adapters.tmux.capture import sanitize_capture


def test_capture_from_a_disposable_tmux_socket_is_cleaned(tmp_path: Path) -> None:
    socket = f"remote-agents-test-{uuid4().hex}"
    session = f"ra-{uuid4()}"
    try:
        subprocess.run(
            [
                "tmux",
                "-L",
                socket,
                "new-session",
                "-d",
                "-s",
                session,
                "-c",
                str(tmp_path),
                "sh",
                "-c",
                "printf '\\033[31mred\\033[0m'; sleep 1",
            ],
            check=True,
        )
        output = subprocess.run(
            ["tmux", "-L", socket, "capture-pane", "-p", "-t", f"={session}:"],
            check=True,
            capture_output=True,
        ).stdout

        assert sanitize_capture(output, max_lines=10, max_bytes=100) == "red"
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-session", "-t", f"={session}:"], check=False)
