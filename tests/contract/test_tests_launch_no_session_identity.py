"""No test passes the managed pane's session id to anything it starts.

On 2026-10-08 the suite was run from inside a managed pane. A live test launched a real Claude,
which inherited the pane's `REMOTE_AGENTS_SESSION_ID`, so its hook wrote a turn marker for the
*pane's* session under its own ownership and never ended it. The pane then read as busy for
good, and a rollover against it failed `predecessor-not-idle`. `tests/conftest.py` removes the
variable for the whole run; this pins that a child process sees none.

Run with the variable set in the outer environment, as the plan's task command does, so the
assertion is about the scrub and not about a shell that never had it.
"""

from __future__ import annotations

import os
import subprocess
import sys

VARIABLE = "REMOTE_AGENTS_SESSION_ID"


def test_the_test_process_carries_no_session_identity() -> None:
    assert VARIABLE not in os.environ


def test_a_child_process_inherits_no_session_identity() -> None:
    seen = subprocess.run(
        [sys.executable, "-c", f"import os; print(os.environ.get({VARIABLE!r}, ''))"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert seen == ""


def test_the_scrub_holds_when_the_shell_that_runs_pytest_carries_one() -> None:
    """The two checks above pass vacuously where the variable was never set, which is CI.

    So the suite is run again in a child with the variable set, selecting only the first check:
    without the scrub in `tests/conftest.py` the child sees the variable and fails.
    """
    outer = os.environ | {VARIABLE: "11111111-1111-4111-8111-111111111111"}
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
            "test_the_test_process_carries_no_session_identity",
        ],
        env=outer,
        capture_output=True,
        text=True,
        check=False,
    )

    assert child.returncode == 0, child.stdout + child.stderr
