"""Pytest controls for explicitly selected, side-effect-contained live profile checks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_TEST_SOCKET_PREFIX = "remote-agents-test-"


def _socket_directory() -> Path:
    """Resolve the directory tmux puts its sockets in, the same way tmux does."""
    return Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}"


def _test_sockets() -> set[Path]:
    directory = _socket_directory()
    if not directory.is_dir():
        return set()
    return {path for path in directory.glob(f"{_TEST_SOCKET_PREFIX}*") if path.is_socket()}


def pytest_sessionstart(session: pytest.Session) -> None:
    """Record pre-existing sockets so cleanup only ever removes this run's own."""
    session.config._remote_agents_sockets_before = _test_sockets()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Tear down the dedicated tmux servers this run started.

    Every generated socket names a real server, and a test that ends without killing one
    leaves it and its socket behind for good: thousands accumulated in /tmp before this
    existed. Only sockets absent at session start are touched, so a concurrent run — or
    the operator's own `remote-agents` server — is never disturbed.
    """
    before = getattr(session.config, "_remote_agents_sockets_before", set())
    for socket in _test_sockets() - before:
        subprocess.run(
            ["tmux", "-L", socket.name, "kill-server"],
            capture_output=True,
            check=False,
        )
        socket.unlink(missing_ok=True)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("remote-agents")
    group.addoption(
        "--profile",
        action="append",
        default=[],
        metavar="PROFILE_ID",
        help="run the selected opt-in live profile qualification (repeatable)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live_profile: opt-in qualification using a generated dedicated tmux socket"
    )
    config.addinivalue_line(
        "markers", "live_telegram: opt-in check against the configured private Telegram bot"
    )
    config.addinivalue_line(
        "markers", "live_acceptance: opt-in audit of owner-driven Telegram lifecycle traces"
    )
