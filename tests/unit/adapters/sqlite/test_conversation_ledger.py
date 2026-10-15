"""Each managed session's latest provider conversation, kept so a restart can resume it.

A fresh launch never pins the agent's own conversation id, so nothing recorded which
conversation a session was in. The ledger is written from the agent's own hooks and read once,
at startup, by the resume pass. It is its own table, never a field on `sessions` (DEC-115: a
session row is rebuilt by position). It keeps an identifier and a time, never anything typed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from remote_agents.adapters.sqlite.conversation_ledger import SQLiteConversationLedger
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS, apply_migrations, current_version

_SESSION = "ra-session_1"
_FIRST = "7a729881-8115-41fb-8613-160182188f40"
_SECOND = "019a3c1e-2b7d-7c41-9a55-0f6d2e8b1c33"
_AT = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


async def test_the_newest_conversation_is_the_one_kept(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "state.sqlite3", migrations=MIGRATIONS)
    try:
        ledger = SQLiteConversationLedger(connection)
        assert await ledger.latest(_SESSION) is None
        await ledger.record(_SESSION, _FIRST, seen_at=_AT)
        await ledger.record(_SESSION, _SECOND, seen_at=_AT + timedelta(minutes=5))
        assert await ledger.latest(_SESSION) == _SECOND
    finally:
        connection.close()


async def test_an_older_sighting_never_replaces_a_newer_one(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "state.sqlite3", migrations=MIGRATIONS)
    try:
        ledger = SQLiteConversationLedger(connection)
        await ledger.record(_SESSION, _SECOND, seen_at=_AT + timedelta(minutes=5))
        await ledger.record(_SESSION, _FIRST, seen_at=_AT)
        assert await ledger.latest(_SESSION) == _SECOND
    finally:
        connection.close()


async def test_newer_is_judged_by_instant_not_by_the_offset_it_arrived_in(tmp_path: Path) -> None:
    """09:30+02:00 is 07:30 UTC, earlier than 09:00 UTC, though it reads later as text."""
    connection = open_database(tmp_path / "state.sqlite3", migrations=MIGRATIONS)
    try:
        ledger = SQLiteConversationLedger(connection)
        await ledger.record(_SESSION, _SECOND, seen_at=_AT)
        east = timezone(timedelta(hours=2))
        await ledger.record(_SESSION, _FIRST, seen_at=datetime(2026, 10, 16, 9, 30, tzinfo=east))
        assert await ledger.latest(_SESSION) == _SECOND
    finally:
        connection.close()


async def test_a_value_that_is_not_an_identifier_is_dropped(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "state.sqlite3", migrations=MIGRATIONS)
    try:
        ledger = SQLiteConversationLedger(connection)
        for value in ("", "has space", "x" * 129, "semi;colon", "line\nbreak"):
            await ledger.record(_SESSION, value, seen_at=_AT)
        await ledger.record("not a session id", _FIRST, seen_at=_AT)
        assert await ledger.latest(_SESSION) is None
        assert connection.execute("SELECT COUNT(*) FROM session_conversations").fetchone()[0] == 0
    finally:
        connection.close()


async def test_each_session_keeps_its_own(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "state.sqlite3", migrations=MIGRATIONS)
    try:
        ledger = SQLiteConversationLedger(connection)
        await ledger.record(_SESSION, _FIRST, seen_at=_AT)
        await ledger.record("ra-session_2", _SECOND, seen_at=_AT)
        assert await ledger.latest(_SESSION) == _FIRST
        assert await ledger.latest("ra-session_2") == _SECOND
    finally:
        connection.close()


#: The migration that adds `session_conversations`.
_LEDGER_MIGRATION = 20


def test_a_store_at_the_previous_version_migrates_to_the_ledger(tmp_path: Path) -> None:
    before = tuple(step for step in MIGRATIONS if step[0] < _LEDGER_MIGRATION)
    connection = open_database(tmp_path / "state.sqlite3", migrations=before)
    try:
        assert "session_conversations" not in _tables(connection)
        apply_migrations(connection, MIGRATIONS)
        assert current_version(connection) == MIGRATIONS[-1][0]
        assert "session_conversations" in _tables(connection)
    finally:
        connection.close()


def _tables(connection) -> set[str]:
    return {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
