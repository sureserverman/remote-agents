"""Recovery drills preserve SQLite evidence and read-only terminal inspection."""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from remote_agents.adapters.sqlite.database import (
    database_is_ready,
    open_database,
    restore_database,
)
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.services import SessionService
from remote_agents.bootstrap import main
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.ports.terminal import TerminalObservation


def test_online_backup_precedes_a_schema_upgrade_and_retains_the_prior_projection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite3"
    first_migration = ((1, "CREATE TABLE marker (value TEXT NOT NULL);"),)
    first = open_database(path, migrations=first_migration)
    first.execute("INSERT INTO marker(value) VALUES ('before-upgrade')")
    first.commit()
    first.close()

    upgraded = open_database(
        path,
        migrations=first_migration + ((2, "CREATE TABLE newer (id INTEGER PRIMARY KEY);"),),
    )
    upgraded.close()

    backup = sqlite3.connect(path.with_suffix(".sqlite3.bak"))
    try:
        assert backup.execute("SELECT value FROM marker").fetchone() == ("before-upgrade",)
        assert (
            backup.execute("SELECT name FROM sqlite_master WHERE name = 'newer'").fetchone() is None
        )
    finally:
        backup.close()


def test_failed_migration_keeps_the_last_committed_schema_and_backup(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    first_migration = ((1, "CREATE TABLE marker (value TEXT NOT NULL);"),)
    open_database(path, migrations=first_migration).close()

    with pytest.raises(sqlite3.OperationalError):
        open_database(path, migrations=first_migration + ((2, "CREATE TABLE broken ("),))

    assert (
        sqlite3.connect(path)
        .execute("SELECT name FROM sqlite_master WHERE name = 'marker'")
        .fetchone()
    )
    assert path.with_suffix(".sqlite3.bak").exists()


def test_busy_database_fails_within_the_configured_bound(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    open_database(path).close()
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            open_database(path, busy_timeout_ms=25)
        assert time.monotonic() - started < 1
    finally:
        blocker.rollback()
        blocker.close()


async def test_corrupt_database_is_preserved_before_verified_backup_restore(tmp_path: Path) -> None:
    source = tmp_path / "good.sqlite3"
    open_database(source).close()
    backup = tmp_path / "sessions.sqlite3.bak"
    source.replace(backup)
    destination = tmp_path / "sessions.sqlite3"
    corrupt = b"not a SQLite database"
    destination.write_bytes(corrupt)
    Path(f"{destination}-wal").write_bytes(b"corrupt WAL evidence")

    restore_database(destination, backup)

    assert destination.with_suffix(".sqlite3.corrupt").read_bytes() == corrupt
    assert destination.with_suffix(".sqlite3.corrupt-wal").read_bytes() == b"corrupt WAL evidence"
    assert database_is_ready(destination)
    SQLiteSessionStore(open_database(destination))


def test_restore_command_uses_the_default_backup_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "good.sqlite3"
    open_database(source).close()
    destination = tmp_path / "sessions.sqlite3"
    source.replace(destination.with_suffix(".sqlite3.bak"))
    destination.write_bytes(b"not a SQLite database")
    monkeypatch.setattr(
        sys, "argv", ["remote-agents", "restore-database", "--database", str(destination)]
    )

    assert main() == 0
    assert database_is_ready(destination)


async def test_terminal_inspection_remains_read_only_when_store_is_unavailable(
    tmp_path: Path,
) -> None:
    terminal, gateway = _terminal(tmp_path)
    session_id = SessionId.new()
    mutation_terminal = CountingTerminal()
    service = SessionService(UnavailableStore(), mutation_terminal)
    try:
        assert (await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))).live

        observation = await terminal.inspect(session_id)
        with pytest.raises(sqlite3.OperationalError, match="unavailable"):
            await service.launch(
                LaunchCommand(ProjectId("opaque-editor"), ProfileId("fake"), "blocked")
            )

        assert observation is not None and observation.session_id == session_id
        assert mutation_terminal.launch_calls == 0
    finally:
        try:
            await gateway.mutate("kill-session", f"ra-{session_id}")
        except RuntimeError:
            pass


def _terminal(tmp_path: Path) -> tuple[TmuxTerminal, TmuxGateway]:
    agent = tmp_path / "fake_agent.py"
    # 30s, not 1s: every test here kills its own session, so the agent only has to
    # outlive the test body. At 1s a loaded run watched the pane die underneath it.
    agent.write_text("import time\nprint('READY', flush=True)\ntime.sleep(30)\n", encoding="utf-8")
    gateway = TmuxGateway(
        f"remote-agents-test-{uuid4().hex}",
        AsyncTmuxRunner(),
        intent_directory=tmp_path / "intents",
    )
    return (
        TmuxTerminal(
            gateway,
            {ProjectId("opaque-editor"): tmp_path},
            {
                ProfileId("fake"): LaunchProfile(
                    sys.executable,
                    (sys.executable, str(agent)),
                    {"PATH": os.environ["PATH"]},
                    "READY",
                )
            },
            # Generous on purpose: line 127 asserts the launch went live, which is a
            # positive-readiness assertion and so the load-sensitive direction (BL-017).
            startup_timeout=5.0,
        ),
        gateway,
    )


class UnavailableStore:
    async def claim_idempotency_key(self, key: str) -> bool:
        raise sqlite3.OperationalError("store unavailable")


class CountingTerminal:
    def __init__(self) -> None:
        self.launch_calls = 0

    async def launch(
        self, session_id: SessionId, project_id: ProjectId, profile_id: ProfileId
    ) -> TerminalObservation:
        self.launch_calls += 1
        return TerminalObservation(session_id, live=True, preserved=False)

    async def inspect(self, session_id: SessionId) -> TerminalObservation | None:
        return None

    async def graceful_stop(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation:
        raise AssertionError("not used")

    async def cleanup(self, session_id: SessionId) -> None:
        raise AssertionError("not used")

    async def force_stop(self, session_id: SessionId) -> TerminalObservation:
        raise AssertionError("not used")
