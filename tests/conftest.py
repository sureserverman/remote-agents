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


def _server_answers(socket: Path) -> bool:
    """Whether a real tmux server is still behind this socket file.

    `has-session` with no target asks whether the server has any session at all, which is the
    cheapest question that distinguishes a live server from an abandoned socket file. A server
    that is up but empty is vanishingly unlikely here -- every test server is created by
    `new-session` -- and treating one as live is the conservative direction anyway.
    """
    return (
        subprocess.run(
            ["tmux", "-L", socket.name, "has-session"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    """Reap sockets no server is behind, then record the rest as not ours to remove.

    The recording half is what keeps `pytest_sessionfinish` from touching a concurrent run's
    server or the operator's own; that rule is unchanged.

    The reaping half closes the leak it left open. `sessionfinish` only removes what *this* run
    created, so a run that crashes, is interrupted, or is killed never reaches it and abandons its
    sockets for good. They accumulated: 185 of them on this host by 2026-08-27, dating back to
    2026-08-05, two still holding a live server from runs whose `tmp_path` had long since gone.

    Only **dead** sockets are reaped, and that is what makes this safe rather than a heuristic
    about age. A socket with no server behind it cannot belong to a concurrent run -- a running
    suite's server answers `has-session` -- so removing one can disturb nothing. A live orphan is
    left alone precisely because "live" and "somebody else's" are indistinguishable from here.

    The prefix is the whole guard on blast radius: `remote-agents-test-` cannot match the bare
    `remote-agents` socket the operator's own server uses, because that name has no such prefix.
    """
    for socket in _test_sockets():
        if not _server_answers(socket):
            socket.unlink(missing_ok=True)
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
    # **What a hosted CI runner cannot do, as a named set rather than an absence.**
    #
    # The meaning is narrow and deliberate: this marks a test whose blocker is a real
    # **login session**. A GitHub runner has no console login, so the `gui/<uid>` domain a
    # LaunchAgent bootstraps into does not exist there -- not "is empty", does not exist -- and
    # no amount of runner configuration creates one. That is the whole population.
    #
    # It is NOT the tree of `REMOTE_AGENTS_LIVE_ACCEPTANCE` tests. Those are held back by
    # credentials and by real agent CLIs, which a runner could in principle be given; they
    # already announce themselves with `BLOCKED:` skips, and folding them in here would make
    # this marker mean "does not run in CI", which is a description of a symptom rather than a
    # reason. A marker that means two things is a marker that can absorb a third, and the third
    # is always a test that started failing.
    config.addinivalue_line(
        "markers",
        "requires_session: needs a real login session (launchd's gui/<uid>), which no hosted "
        "CI runner has; excluded by name from the CI matrix so a green badge does not claim it",
    )
