"""Which Codex daemon is running, read from the pid file Codex itself writes.

A restarted daemon has forgotten every thread's settings, so the service re-applies a preset
when the daemon is a different one. The reading is a digest of the file's process fields, not
the fields: it is stored beside each session and must carry nothing a path or a log could.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from remote_agents.adapters.agents.codex.thread_settings import CodexThreadPermissions

#: The file as codex 0.167.2 writes it (measured 2026-11-12), digests shortened.
MEASURED = {
    "pid": 1259718,
    "processStartTime": "Thu Nov 12 12:30:23 2026",
    "processIdentity": {"bootId": "bff7c0d2-e336-449d-859a-44d25286df29", "startTicks": 62581359},
    "executableIdentity": {"digest": [161, 216], "path_digest": [187, 163]},
}


def _home(tmp_path: Path, content: object) -> Path:
    directory = tmp_path / "app-server-daemon"
    directory.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    (directory / "app-server.pid").write_text(text, encoding="utf-8")
    return tmp_path


async def _identity(home: Path) -> str | None:
    return await CodexThreadPermissions(home=home).server_identity()


async def test_the_same_daemon_reads_the_same_each_time(tmp_path) -> None:
    home = _home(tmp_path, MEASURED)

    first = await _identity(home)

    assert first is not None
    assert first == await _identity(home)


@pytest.mark.parametrize(
    "restarted",
    [
        {**MEASURED, "pid": 1259719},
        {**MEASURED, "processStartTime": "Thu Nov 12 12:31:00 2026"},
        # The same pid again after a reboot, or reused later in the same boot.
        {**MEASURED, "processIdentity": {**MEASURED["processIdentity"], "bootId": "another"}},
        {**MEASURED, "processIdentity": {**MEASURED["processIdentity"], "startTicks": 1}},
    ],
)
async def test_a_restarted_daemon_reads_differently(tmp_path, restarted) -> None:
    before = await _identity(_home(tmp_path / "before", MEASURED))
    after = await _identity(_home(tmp_path / "after", restarted))

    assert after is not None
    assert after != before


async def test_an_updated_binary_alone_is_not_a_restart(tmp_path) -> None:
    """The executable's digest is not the process: only a new process forgets the settings."""
    rebuilt = {**MEASURED, "executableIdentity": {"digest": [1], "path_digest": [2]}}

    assert await _identity(_home(tmp_path / "a", MEASURED)) == await _identity(
        _home(tmp_path / "b", rebuilt)
    )


async def test_the_reading_carries_nothing_the_file_said(tmp_path) -> None:
    identity = await _identity(_home(tmp_path, MEASURED))

    assert identity is not None
    assert len(identity) == 32
    assert set(identity) <= set("0123456789abcdef")
    for said in ("1259718", "bff7c0d2", "62581359", "Nov"):
        assert said not in identity


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "[1, 2]",
        "{}",
        json.dumps({"pid": "1259718", "processStartTime": "Thu Nov 12 12:30:23 2026"}),
        json.dumps({"pid": True, "processStartTime": "Thu Nov 12 12:30:23 2026"}),
        # A pid with no start is not a process identity: pids are reused.
        json.dumps({"pid": 1259718}),
        json.dumps({"pid": 1259718, "processIdentity": {"bootId": 7, "startTicks": "x"}}),
        "[" * 100_000,
        json.dumps({**MEASURED, "padding": "x" * 70_000}),
    ],
)
async def test_a_file_that_is_not_a_daemon_record_is_no_reading(tmp_path, content) -> None:
    assert await _identity(_home(tmp_path, content)) is None


async def test_no_file_is_no_reading(tmp_path) -> None:
    assert await _identity(tmp_path) is None


async def test_a_link_in_place_of_the_file_is_no_reading(tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere.pid"
    elsewhere.write_text(json.dumps(MEASURED), encoding="utf-8")
    directory = tmp_path / "home" / "app-server-daemon"
    directory.mkdir(parents=True)
    (directory / "app-server.pid").symlink_to(elsewhere)

    assert await _identity(tmp_path / "home") is None


async def test_a_file_owned_by_another_user_is_no_reading(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, MEASURED)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(home).st_uid + 1)

    assert await _identity(home) is None


async def test_the_home_follows_codex_home_as_codex_does(tmp_path, monkeypatch) -> None:
    """A drill's private home must read its own daemon, never the owner's."""
    drill = _home(tmp_path / "drill", {**MEASURED, "pid": 42})
    monkeypatch.setenv("CODEX_HOME", str(drill))

    assert await CodexThreadPermissions().server_identity() == await _identity(drill)

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nowhere"))
    assert await CodexThreadPermissions().server_identity() is None


async def test_a_home_that_cannot_be_resolved_is_no_reading(monkeypatch) -> None:
    """`~nobody-by-this-name` cannot be expanded, and that is a missing reading, not a crash."""
    monkeypatch.setenv("CODEX_HOME", "~no-such-user-for-this-test/codex")

    assert await CodexThreadPermissions().server_identity() is None


async def test_a_directory_in_place_of_the_file_is_no_reading(tmp_path) -> None:
    (tmp_path / "app-server-daemon" / "app-server.pid").mkdir(parents=True)

    assert await _identity(tmp_path) is None


def _write(home: Path, name: str, content: object) -> None:
    directory = home / "app-server-daemon"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(content), encoding="utf-8")


def _daemon_owned(home: Path) -> None:
    """`<home>/packages/app-server-daemon/current`, which a daemon-owned install links in."""
    package = home / "packages" / "app-server-daemon"
    (package / "0.169.0").mkdir(parents=True)
    (package / "current").symlink_to(package / "0.169.0")


async def test_a_daemon_owned_install_is_read_from_daemon_pid(tmp_path) -> None:
    """Measured 2026-11-13 in the drill: Codex 0.169 with its daemon's own package writes
    `daemon.pid`; only a binary under `packages/standalone` keeps `app-server.pid`
    (`codex-rs/app-server-daemon/src/lib.rs`, `managed_install.rs`)."""
    _daemon_owned(tmp_path)
    _write(tmp_path, "daemon.pid", MEASURED)

    assert await _identity(tmp_path) == await _identity(_home(tmp_path / "legacy", MEASURED))


async def test_the_daemon_owned_file_wins_over_a_legacy_one_left_behind(tmp_path) -> None:
    _daemon_owned(tmp_path)
    _write(tmp_path, "app-server.pid", {**MEASURED, "pid": 1})
    _write(tmp_path, "daemon.pid", MEASURED)

    assert await _identity(tmp_path) == await _identity(_home(tmp_path / "legacy", MEASURED))


async def test_daemon_artifacts_alone_name_daemon_pid(tmp_path) -> None:
    """No package link yet: Codex looks for its own artifacts, the daemon's first."""
    _write(tmp_path, "app-server.pid", {**MEASURED, "pid": 1})
    (tmp_path / "app-server-daemon" / "daemon.stderr.log").write_text("", encoding="utf-8")
    _write(tmp_path, "daemon.pid", MEASURED)

    assert await _identity(tmp_path) == await _identity(_home(tmp_path / "legacy", MEASURED))


async def test_a_legacy_home_with_a_daemon_pid_beside_it_reads_app_server_pid(tmp_path) -> None:
    """The owner's home: a standalone binary, legacy artifacts, and nothing the daemon owns."""
    home = _home(tmp_path, MEASURED)
    (home / "app-server-daemon" / "app-server.stderr.log").write_text("", encoding="utf-8")

    assert await _identity(home) is not None
    assert await _identity(home) == await _identity(_home(tmp_path / "again", MEASURED))
