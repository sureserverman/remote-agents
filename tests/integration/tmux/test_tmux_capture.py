"""Real dedicated-socket capture is passed through the safety boundary."""

import subprocess
import time
from pathlib import Path
from uuid import uuid4

from remote_agents.application.captures import render_capture


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
                # `sleep 30`, not `sleep 1`. The pane has to outlive the poll below, and a
                # one-second window is not a window -- it made the pane's own lifetime a second
                # race running the opposite way from the first.
                "printf '\\033[31mred\\033[0m'; sleep 30",
            ],
            check=True,
        )

        # Polled, because `new-session -d` returns when tmux has STARTED the shell, not when
        # the shell has written anything. Those are different instants, and the gap is
        # scheduling -- so the original single capture was a race that this Linux workstation
        # simply always won. The macOS runner lost it on the first try, capturing an empty
        # pane and asserting `'' == 'red'`.
        #
        # The same poll-to-a-deadline shape `_reaped` and `process_gone` already use elsewhere
        # in this suite, and for the same underlying reason: nothing here is synchronous with
        # the process being observed.
        deadline = time.monotonic() + 10.0
        capture = None
        while time.monotonic() < deadline:
            output = subprocess.run(
                ["tmux", "-L", socket, "capture-pane", "-p", "-t", f"{session}:"],
                check=True,
                capture_output=True,
            ).stdout
            capture = render_capture(output, max_lines=10, max_bytes=100)
            if capture.text:
                break
            time.sleep(0.05)

        assert capture is not None and capture.text == "red"
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-session", "-t", f"={session}:"], check=False)
