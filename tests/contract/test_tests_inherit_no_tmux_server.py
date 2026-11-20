"""No test inherits the tmux server of the pane the suite runs in (DEC-144).

Since DEC-144 a surface builds its gateway and console composer on the server `$TMUX` names.
Run from a pane of the owner's console, the suite carries `TMUX` naming `remote-agents`, so a
test composing a surface would be console-hosted on the owner's real console and run `ensure`
and the start-time repair against it. `tests/conftest.py` removes `TMUX` and `TMUX_PANE` for
the whole run; this pins that, the way `test_tests_launch_no_session_identity.py` pins the
session id: once in this process, once in a child, and once in a child pytest started with
both set, so the assertion is about the scrub and not about a shell that never had them.
"""

from __future__ import annotations

import os
import subprocess
import sys

VARIABLES = ("TMUX", "TMUX_PANE")


def test_the_test_process_carries_no_tmux_server() -> None:
    assert not [name for name in VARIABLES if name in os.environ]


def test_a_child_process_inherits_no_tmux_server() -> None:
    seen = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import os; print(','.join(n for n in {VARIABLES!r} if n in os.environ))",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert seen == ""


def test_the_scrub_holds_when_the_shell_that_runs_pytest_is_in_the_owner_s_console() -> None:
    """The two checks above pass vacuously where tmux was never around, which is CI."""
    outer = os.environ | {
        "TMUX": "/tmp/tmux-1000/remote-agents,4242,0",
        "TMUX_PANE": "%7",
    }
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            __file__,
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:xdist",
            "-k",
            "test_the_test_process_carries_no_tmux_server",
        ],
        env=outer,
        capture_output=True,
        text=True,
        check=False,
    )

    assert child.returncode == 0, child.stdout + child.stderr
