"""Recovery drills preserve SQLite evidence and read-only terminal inspection."""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from remote_agents.adapters.sqlite import database as database_module
from remote_agents.adapters.sqlite.database import (
    backup_path,
    database_is_ready,
    open_database,
    restore_database,
)
from remote_agents.adapters.sqlite.migrations import MIGRATIONS, current_version
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
        assert (
            await terminal.launch(session_id, ProjectId("opaque-editor"), ProfileId("fake"))
        ).live

        observation = await terminal.inspect(session_id)
        with pytest.raises(sqlite3.OperationalError, match="unavailable"):
            (
                await service.launch(
                    LaunchCommand(ProjectId("opaque-editor"), ProfileId("fake"), "blocked")
                )
            ).record

        assert observation is not None and observation.session_id == session_id
        assert mutation_terminal.launch_calls == 0
    finally:
        try:
            await gateway.destroy(session_id)
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
        self,
        session_id: SessionId,
        project_id: ProjectId,
        profile_id: ProfileId,
        *,
        remote_control: bool = False,
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


def test_restore_refuses_to_roll_a_database_back_over_a_newer_schema(tmp_path, monkeypatch) -> None:
    """The documented recovery procedure used to destroy data on a rollback.

    `database_is_ready` means "at the newest schema *this binary* knows", so an **older**
    build reads a migrated database as unready. `doctor` then reports it unready,
    `docs/database-recovery.md` says the restore command refuses to overwrite a healthy
    database and hands over the invocation, and the restore moved the newer database aside
    as `.corrupt` and reinstated the pre-migration snapshot — losing every session recorded
    since the upgrade, on a command documented as safe.

    The setup is the real production sequence rather than a contrivance: `open_database`
    snapshots to `.bak` before applying a migration, so upgrading leaves a previous-schema
    backup beside a current-schema database exactly as staged here. The rolled-back build is
    simulated by truncating the migration list the module reads, which is precisely what
    distinguishes the two builds — the stored version is read from the database either way.
    """
    path = tmp_path / "sessions.sqlite3"
    open_database(path, migrations=MIGRATIONS[:-1]).close()
    open_database(path).close()

    newest, previous = MIGRATIONS[-1][0], MIGRATIONS[-2][0]
    assert current_version(sqlite3.connect(path)) == newest
    assert current_version(sqlite3.connect(backup_path(path))) == previous, (
        "the upgrade should have snapshotted the pre-migration schema"
    )

    # The rolled-back build knows one migration fewer, so it calls the live database unready
    # while the older snapshot beside it looks perfectly healthy.
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:-1])

    assert database_is_ready(path) is False, "the premise: the old build calls this unhealthy"
    assert database_is_ready(backup_path(path)) is True, "and calls the stale snapshot healthy"

    with pytest.raises(ValueError, match="newer schema"):
        restore_database(path)

    monkeypatch.undo()
    assert current_version(sqlite3.connect(path)) == newest, "the live database is untouched"
    assert not path.with_suffix(".sqlite3.corrupt").exists(), (
        "nothing may be moved aside when the restore was refused"
    )


def _ui_store_with_a_backup(directory: Path) -> tuple[Path, Path]:
    """A `ui.sqlite3` and a backup of it at the UI head, as `open_ui_database` leaves them."""
    from remote_agents.adapters.sqlite.database import ui_database_path
    from remote_agents.adapters.sqlite.migrations import UI_MIGRATIONS

    ui = ui_database_path(directory / "sessions.sqlite3")
    good = directory / "good-ui.sqlite3"
    open_database(good, migrations=UI_MIGRATIONS).close()
    backup = backup_path(ui)
    good.replace(backup)
    return ui, backup


def test_a_corrupt_ui_store_is_restored_from_its_backup(tmp_path: Path) -> None:
    """BL-092: a UI backup is at the UI head, never the domain head, and used to be refused."""
    from remote_agents.adapters.sqlite.migrations import UI_MIGRATIONS

    ui, backup = _ui_store_with_a_backup(tmp_path)
    ui.write_bytes(b"not a SQLite database")

    restore_database(ui, backup)

    assert database_is_ready(ui, UI_MIGRATIONS)
    assert ui.with_suffix(".sqlite3.corrupt").read_bytes() == b"not a SQLite database"


def test_a_healthy_ui_store_is_never_replaced(tmp_path: Path) -> None:
    from remote_agents.adapters.sqlite.migrations import UI_MIGRATIONS

    ui, backup = _ui_store_with_a_backup(tmp_path)
    open_database(ui, migrations=UI_MIGRATIONS).close()

    with pytest.raises(ValueError, match="healthy"):
        restore_database(ui, backup)


def test_a_ui_store_ahead_of_its_backup_is_never_rolled_back(tmp_path: Path, monkeypatch) -> None:
    """The newer-schema refusal holds for the UI store too, comparing UI with UI."""
    from remote_agents.adapters.sqlite.migrations import UI_MIGRATIONS

    ui, backup = _ui_store_with_a_backup(tmp_path)
    open_database(backup, migrations=UI_MIGRATIONS[:-1]).close()
    backup.unlink()
    open_database(ui, migrations=UI_MIGRATIONS[:-1]).close()
    open_database(ui, migrations=UI_MIGRATIONS).close()  # leaves the older snapshot as `.bak`
    # A build one UI migration behind: the live store is ahead of the backup it would restore.
    monkeypatch.setattr(database_module, "UI_MIGRATIONS", UI_MIGRATIONS[:-1])

    with pytest.raises(ValueError, match="newer schema"):
        restore_database(ui, backup_path(ui))


def test_the_restore_command_restores_the_ui_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from remote_agents.adapters.sqlite.migrations import UI_MIGRATIONS

    ui, _backup = _ui_store_with_a_backup(tmp_path)
    ui.write_bytes(b"not a SQLite database")
    monkeypatch.setattr(sys, "argv", ["remote-agents", "restore-database", "--database", str(ui)])

    assert main() == 0
    assert database_is_ready(ui, UI_MIGRATIONS)


def test_a_domain_store_at_the_ui_version_is_never_restored_over_the_ui_store(
    tmp_path: Path,
) -> None:
    """A version number alone does not say which store a file is (Tier-1 review, BL-092)."""
    ui, _backup = _ui_store_with_a_backup(tmp_path)
    ui.write_bytes(b"not a SQLite database")
    old_domain = tmp_path / "old-domain.sqlite3"
    open_database(old_domain, migrations=MIGRATIONS[:2]).close()  # version 2, like the UI head

    with pytest.raises(ValueError, match="not a backup of the surface store"):
        restore_database(ui, old_domain)
    assert ui.read_bytes() == b"not a SQLite database", "the UI store was replaced anyway"


def test_a_ui_store_is_never_restored_over_the_domain_store(tmp_path: Path) -> None:
    from remote_agents.adapters.sqlite.migrations import UI_MIGRATIONS

    domain = tmp_path / "sessions.sqlite3"
    domain.write_bytes(b"not a SQLite database")
    ui_copy = tmp_path / "ui-copy.sqlite3"
    open_database(ui_copy, migrations=UI_MIGRATIONS).close()

    with pytest.raises(ValueError):
        restore_database(domain, ui_copy)
    assert domain.read_bytes() == b"not a SQLite database"


def _store_and_list(directory: Path, kind: str) -> tuple[Path, tuple[tuple[int, str], ...]]:
    """The store path `restore_database` judges by name, and the migration list it uses."""
    from remote_agents.adapters.sqlite.database import ui_database_path
    from remote_agents.adapters.sqlite.migrations import UI_MIGRATIONS

    domain = directory / "sessions.sqlite3"
    if kind == "ui":
        return ui_database_path(domain), UI_MIGRATIONS
    return domain, MIGRATIONS


def _upgraded_with_a_marker(path: Path, migrations: tuple[tuple[int, str], ...]) -> None:
    """The production sequence: a store one release old, then the upgrade that wrote `.bak`.

    The marker row is written before the upgrade, so it is in the `.bak` the upgrade left --
    a restored store carrying it is the backup's content, not a fresh empty store.
    """
    older = open_database(path, migrations=migrations[:-1])
    older.execute("CREATE TABLE restore_marker (value TEXT NOT NULL)")
    older.execute("INSERT INTO restore_marker(value) VALUES ('from-the-backup')")
    older.commit()
    older.close()
    open_database(path, migrations=migrations).close()


@pytest.mark.parametrize("kind", ["domain", "ui"])
def test_a_backup_one_schema_behind_is_restored_and_migrated(tmp_path: Path, kind: str) -> None:
    """BL-129: the `.bak` an upgrade writes is one schema behind, and is the default source."""
    path, migrations = _store_and_list(tmp_path, kind)
    _upgraded_with_a_marker(path, migrations)
    head = migrations[-1][0]
    assert current_version(sqlite3.connect(backup_path(path))) == head - 1, "the premise"
    path.write_bytes(b"not a SQLite database")

    restore_database(path)

    assert database_is_ready(path, migrations)
    restored = sqlite3.connect(path)
    try:
        assert current_version(restored) == head
        assert restored.execute("SELECT value FROM restore_marker").fetchone() == (
            "from-the-backup",
        )
    finally:
        restored.close()
    assert path.with_suffix(".sqlite3.corrupt").read_bytes() == b"not a SQLite database"
    assert not path.with_suffix(".sqlite3.tmp").exists()


@pytest.mark.parametrize("kind", ["domain", "ui"])
def test_a_backup_two_schemas_behind_is_refused_by_name(tmp_path: Path, kind: str) -> None:
    path, migrations = _store_and_list(tmp_path, kind)
    head = migrations[-1][0]
    old = tmp_path / "old.sqlite3"
    if head - 2 > 0:
        open_database(old, migrations=migrations[: head - 2]).close()
        version = head - 2
    else:
        # The UI head is 2, so "two behind" is a file carrying no UI migration at all.
        old.write_bytes(b"")
        version = 0
    path.write_bytes(b"not a SQLite database")

    with pytest.raises(ValueError) as refused:
        restore_database(path, old)

    if version > 0:
        assert str(refused.value) == (
            f"database backup is two or more schema versions behind "
            f"(v{version}; this release is v{head})"
        )
    else:
        # The UI head is 2, so its "two behind" carries no UI schema at all: that is the
        # unreadable refusal, and the two messages must stay distinct.
        assert str(refused.value) == "database backup is not a readable current schema"
    assert path.read_bytes() == b"not a SQLite database"
    assert not path.with_suffix(".sqlite3.corrupt").exists()


def test_a_failed_forward_migration_leaves_the_target_and_corrupt_copy_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy is migrated in its temporary file before anything is moved or published.

    Migrating after preserving would leave no store at all; after publishing, a half-migrated
    one. The head migration here raises, and the refusal must be that error -- not the
    readiness refusal a one-behind backup used to meet, which never reached a migration.
    """
    path = tmp_path / "sessions.sqlite3"
    open_database(path, migrations=MIGRATIONS[:-1]).close()
    path.replace(backup_path(path))
    path.write_bytes(b"not a SQLite database")
    Path(f"{path}-wal").write_bytes(b"WAL evidence")
    head = MIGRATIONS[-1][0]
    monkeypatch.setattr(
        database_module, "MIGRATIONS", (*MIGRATIONS[:-1], (head, "CREATE TABLE sessions (x);"))
    )

    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        restore_database(path)

    assert path.read_bytes() == b"not a SQLite database"
    assert Path(f"{path}-wal").read_bytes() == b"WAL evidence"
    assert not path.with_suffix(".sqlite3.corrupt").exists()
    assert not path.with_suffix(".sqlite3.tmp").exists()
    assert current_version(sqlite3.connect(backup_path(path))) == head - 1, "source untouched"


@pytest.mark.parametrize("kind", ["domain", "ui"])
def test_a_restored_store_is_never_world_readable(tmp_path: Path, kind: str) -> None:
    """Every copy `_backup_connection` publishes is owner-only, whatever the umask.

    The UI store holds live callback tokens. Its restored file and the `.bak` each upgrade
    writes used to be published at the process umask -- 0644 here -- until the next open
    narrowed the live file, and nothing ever narrowed the `.bak`.
    """
    path, migrations = _store_and_list(tmp_path, kind)
    previous = os.umask(0o022)
    try:
        _upgraded_with_a_marker(path, migrations)
        assert backup_path(path).stat().st_mode & 0o077 == 0, "the upgrade's .bak"
        path.write_bytes(b"not a SQLite database")
        restore_database(path)
    finally:
        os.umask(previous)

    assert path.stat().st_mode & 0o077 == 0, "the restored store"


def test_the_forward_migration_never_crosses_the_store_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migration 14 drops the UI tables and may only run after `split_stores` copied them.

    Pinned through `restore_database` itself, not by re-deriving its window: a one-behind
    restore runs the head migration alone, and a backup from before the split is refused
    before any migration runs. Widening the window would fail one half or the other.
    """
    assert "DROP TABLE IF EXISTS callback_states" in dict(MIGRATIONS)[14], (
        "migration 14 is no longer the split; re-derive which migration this guards"
    )
    ran: list[int] = []
    real = database_module.apply_migrations

    def recording(connection, migrations=MIGRATIONS):
        before = current_version(connection)
        real(connection, migrations)
        ran.extend(version for version, _ in migrations if version > before)

    head = MIGRATIONS[-1][0]
    path = tmp_path / "sessions.sqlite3"
    open_database(path, migrations=MIGRATIONS[:-1]).close()
    path.replace(backup_path(path))
    path.write_bytes(b"not a SQLite database")
    pre_split = tmp_path / "pre-split.sqlite3"
    open_database(pre_split, migrations=MIGRATIONS[:13]).close()
    damaged = tmp_path / "other" / "sessions.sqlite3"
    damaged.parent.mkdir()
    damaged.write_bytes(b"not a SQLite database")
    # Installed after the setup, whose own `open_database` calls migrate through it too.
    monkeypatch.setattr(database_module, "apply_migrations", recording)

    restore_database(path)
    assert ran == [head]

    ran.clear()
    with pytest.raises(ValueError, match="two or more schema versions behind"):
        restore_database(damaged, pre_split)
    assert ran == [], "a pre-split backup reached a migration"
