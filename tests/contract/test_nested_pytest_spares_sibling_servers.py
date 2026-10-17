"""A pytest run by a test never tears down a tmux server it did not start (BL-100).

`tests/conftest.py` kills, at session finish, every `remote-agents-test-*` server created since
session start. Two contract tests run pytest again in a child (`test_ci_claims_no_more_than_it_runs`
collects the whole suite three times; `test_tests_launch_no_session_identity` runs one check).
Each child loaded the same hooks, and a child is not an xdist worker, so its finish killed every
test server a *sibling worker* had started while it ran: whichever real-tmux test was mid-flight
lost its server, in any file. That was BL-100 -- measured 2026-10-17 by creating a server every
0.1 s during a child collection: the eleven created inside its session all died.

This drives the real hooks: a child pytest with a plugin that, mid-session, starts a server the
way a sibling worker would, and the server must still answer after the child exits.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

_REPOSITORY = Path(__file__).resolve().parents[2]

_SIBLING = """
import subprocess


def pytest_collection_finish(session):
    # Inside the child's session: after its start recorded the sockets, before its finish.
    subprocess.run(
        ["tmux", "-L", "{socket}", "new-session", "-d", "-s", "sibling", "sleep 600"],
        check=True,
    )
"""


@pytest.fixture
def socket() -> str:
    if shutil.which("tmux") is None:
        pytest.skip("BLOCKED: tmux is not installed")
    name = f"remote-agents-test-sibling-{uuid.uuid4().hex[:8]}"
    yield name
    subprocess.run(["tmux", "-L", name, "kill-server"], capture_output=True, check=False)


def test_a_child_pytest_leaves_a_server_started_during_its_session_alone(
    socket: str, tmp_path: Path
) -> None:
    (tmp_path / "sibling_plugin.py").write_text(_SIBLING.format(socket=socket), encoding="utf-8")
    environment = os.environ | {
        "PYTHONPATH": os.pathsep.join(filter(None, (str(tmp_path), os.environ.get("PYTHONPATH"))))
    }
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit/test_package.py",
            "--collect-only",
            "-q",
            "-p",
            "sibling_plugin",
            "-p",
            "no:randomly",
            "-p",
            "no:xdist",
        ],
        cwd=_REPOSITORY,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stdout + child.stderr

    alive = subprocess.run(["tmux", "-L", socket, "has-session"], capture_output=True, check=False)
    assert alive.returncode == 0, "the child's session finish killed a server it did not start"
