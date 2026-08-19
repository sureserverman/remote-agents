"""The leased connection holds no database handle between store operations.

This is the position DEC-023 declined to state, stated: the local surface becomes
long-lived beside attached sessions, and what keeps DEC-005's two-writer story simple is
that the surface's handle exists only for the duration of a single store operation. The
lease is a drop-in for the `sqlite3.Connection` surface `SQLiteSessionStore` actually
uses — bare `execute(...).fetch*()` reads, and `with connection:` transaction blocks —
so the store itself never learns which composition it is running under. The serve
composition keeps its long-lived connection and never sees this class.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import LeasedConnection, open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


class TrackingOpener:
    """Opens real connections while recording how many are alive right now."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.open_now = 0
        self.opened_total = 0

    def __call__(self) -> sqlite3.Connection:
        opener = self

        class _Tracked(sqlite3.Connection):
            def close(self) -> None:  # noqa: D102 — sqlite3 signature
                opener.open_now -= 1
                super().close()

        connection = sqlite3.connect(self._path, factory=_Tracked)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 1000")
        opener.open_now += 1
        opener.opened_total += 1
        return connection


def _record() -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        SessionState.STARTING,
        datetime.now(UTC),
    )


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "state.sqlite3"
    open_database(path, migrations=MIGRATIONS).close()
    return path


async def test_no_handle_survives_between_two_store_operations(database: Path) -> None:
    opener = TrackingOpener(database)
    store = SQLiteSessionStore(LeasedConnection(opener))

    record = _record()
    await store.save(record)
    assert opener.open_now == 0, "a handle survived the write operation"

    loaded = await store.get(record.session_id)
    assert opener.open_now == 0, "a handle survived the read operation"
    assert loaded is not None and loaded.session_id == record.session_id
    assert opener.opened_total >= 2, "operations must not share one hidden connection"


async def test_writes_commit_and_are_visible_to_an_independent_connection(
    database: Path,
) -> None:
    store = SQLiteSessionStore(LeasedConnection(TrackingOpener(database)))
    record = _record()
    await store.save(record)

    other = sqlite3.connect(database)
    try:
        rows = other.execute("SELECT session_id FROM sessions").fetchall()
    finally:
        other.close()
    assert rows == [(str(record.session_id),)]


async def test_a_failed_transaction_rolls_back_and_still_releases_the_handle(
    database: Path,
) -> None:
    opener = TrackingOpener(database)
    lease = LeasedConnection(opener)

    with pytest.raises(sqlite3.OperationalError):
        with lease:
            lease.execute(
                "INSERT INTO idempotency_claims(key, created_at) VALUES (?, ?)",
                ("k1", datetime.now(UTC).isoformat()),
            )
            lease.execute("INSERT INTO no_such_table VALUES (1)")
    assert opener.open_now == 0, "a handle survived the failed transaction"

    other = sqlite3.connect(database)
    try:
        rows = other.execute("SELECT key FROM idempotency_claims").fetchall()
    finally:
        other.close()
    assert rows == [], "a rolled-back write reached the database"


async def test_a_bare_write_outside_a_transaction_lands_durably(database: Path) -> None:
    """The lease may never lose a write to autocommit limbo: an execute outside a `with`
    block commits before the per-operation connection closes."""
    opener = TrackingOpener(database)
    lease = LeasedConnection(opener)
    lease.execute(
        "INSERT INTO idempotency_claims(key, created_at) VALUES (?, ?)",
        ("bare", datetime.now(UTC).isoformat()),
    )
    assert opener.open_now == 0

    other = sqlite3.connect(database)
    try:
        rows = other.execute("SELECT key FROM idempotency_claims").fetchall()
    finally:
        other.close()
    assert rows == [("bare",)]


async def test_the_eager_cursor_answers_fetchone_and_fetchall(database: Path) -> None:
    lease = LeasedConnection(TrackingOpener(database))
    lease.execute(
        "INSERT INTO idempotency_claims(key, created_at) VALUES (?, ?)",
        ("a", datetime.now(UTC).isoformat()),
    )
    assert lease.execute("SELECT key FROM idempotency_claims").fetchone() == ("a",)
    assert lease.execute("SELECT key FROM idempotency_claims").fetchall() == [("a",)]
    assert (
        lease.execute("SELECT key FROM idempotency_claims WHERE key = 'missing'").fetchone() is None
    )


async def test_close_is_a_safe_no_op_with_nothing_held(database: Path) -> None:
    """bootstrap's `finally: connection.close()` must hold for either composition."""
    opener = TrackingOpener(database)
    lease = LeasedConnection(opener)
    lease.close()
    assert opener.open_now == 0


async def test_nested_transactions_fail_loud_instead_of_committing_early(
    database: Path,
) -> None:
    """sqlite3 has no nested transactions: an inner exit would commit work the outer block
    still believes it can roll back. The store never nests; anyone who starts must hear it."""
    opener = TrackingOpener(database)
    lease = LeasedConnection(opener)
    with lease:
        with pytest.raises(RuntimeError, match="do not nest"):
            with lease:
                pass
    assert opener.open_now == 0


async def test_a_transaction_belongs_to_the_task_that_opened_it(database: Path) -> None:
    """No store method awaits inside a `with` block today — this pins what happens the day
    one does: a second coroutine reaching the lease mid-transaction fails loudly instead of
    silently joining (or closing) a stranger's transaction."""
    import asyncio

    lease = LeasedConnection(TrackingOpener(database))
    started = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        with lease:
            lease.execute(
                "INSERT INTO idempotency_claims(key, created_at) VALUES (?, ?)",
                ("held", datetime.now(UTC).isoformat()),
            )
            started.set()
            await release.wait()  # the hazard: yielding mid-transaction

    async def intruder() -> None:
        await started.wait()
        with pytest.raises(RuntimeError, match="belongs to the task"):
            lease.execute("SELECT 1")
        release.set()

    await asyncio.gather(holder(), intruder())
