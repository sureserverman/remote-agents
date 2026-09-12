"""The standing trust question, remembered across restarts, against the real schema.

`TrustNotifier` must not ask the same question twice. The thing that stops it is a row, and a
row is only worth what the schema and the adapter actually do — so this drives the real SQLite
store rather than an in-memory stand-in, for the reason
`test_trust_through_the_real_service.py` gives at length: a fake that omits the layer holding
the bug cannot see the bug.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS, current_version
from remote_agents.adapters.sqlite.trust_notifications import SQLiteTrustNotificationStore
from remote_agents.domain.models import SessionId


def _store(tmp_path: Path) -> SQLiteTrustNotificationStore:
    return SQLiteTrustNotificationStore(open_database(tmp_path / "sessions.sqlite3"))


def test_a_fresh_database_migrates_all_the_way_to_the_last_migration(tmp_path: Path) -> None:
    """Compared against `len(MIGRATIONS)`, not against a literal.

    The literal lives in exactly one place on purpose --
    `test_agent_activity_store.test_the_migration_count_is_pinned_by_hand` -- whose docstring
    says to bump that one and only that one. A second hand-written count here would make every
    new migration a two-file edit and would fail for a reason that has nothing to do with
    trust notifications, which is what it did when migration 13 landed.
    """
    connection = open_database(tmp_path / "sessions.sqlite3")

    assert current_version(connection) == len(MIGRATIONS)


def test_a_database_at_eleven_gains_the_table_without_disturbing_its_rows(
    tmp_path: Path,
) -> None:
    """The upgrade path, which is the one a live host actually takes.

    A fresh database proves the statements are valid; it does not prove they can be applied to
    a database that already holds sessions. This stops at 11, writes a row, then migrates.
    """
    path = tmp_path / "sessions.sqlite3"
    connection = sqlite3.connect(path)
    from remote_agents.adapters.sqlite.migrations import apply_migrations

    apply_migrations(connection, [entry for entry in MIGRATIONS if entry[0] <= 11])
    connection.execute(
        "INSERT INTO standing_notifications"
        "(chat_id, session_id, message_id, token, activities, updated_at)"
        " VALUES (1, 'abc', 2, 't', '[]', 'now')"
    )
    connection.commit()
    connection.close()

    migrated = open_database(path)

    assert current_version(migrated) == len(MIGRATIONS)
    assert migrated.execute("SELECT COUNT(*) FROM standing_notifications").fetchone()[0] == 1
    assert migrated.execute("SELECT COUNT(*) FROM trust_notifications").fetchone()[0] == 0


async def test_a_remembered_question_is_read_back(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = SessionId.new()

    await store.remember(session, chat_id=11, message_id=42)
    standing = await store.standing_for(session)

    assert standing is not None
    assert (standing.chat_id, standing.message_id, standing.settled) == (11, 42, False)


async def test_a_session_nobody_asked_about_has_no_standing_row(tmp_path: Path) -> None:
    assert await _store(tmp_path).standing_for(SessionId.new()) is None


async def test_settling_keeps_the_row_so_the_message_can_still_be_named(
    tmp_path: Path,
) -> None:
    """DEC-034 amends the message in place, so the row has to outlive the answer.

    Deleting it would leave the next pass unable to tell "answered" from "never asked" — the
    same absence, and it would send a second copy of a question with one answer.
    """
    store = _store(tmp_path)
    session = SessionId.new()
    await store.remember(session, chat_id=11, message_id=42)

    await store.settle(session)
    standing = await store.standing_for(session)

    assert standing is not None, "settling must not delete the row"
    assert standing.settled is True
    assert standing.message_id == 42, "the amended message is still named"


async def test_remembering_the_same_session_twice_keeps_one_row(tmp_path: Path) -> None:
    """One question per session, enforced by the schema and not by the caller's care."""
    store = _store(tmp_path)
    session = SessionId.new()

    await store.remember(session, chat_id=11, message_id=42)
    await store.remember(session, chat_id=11, message_id=43)
    standing = await store.standing_for(session)

    assert standing is not None
    assert standing.message_id == 43, "the later render is the one that stands"


async def test_the_unsettled_rows_are_what_a_pass_walks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asked, answered = SessionId.new(), SessionId.new()
    await store.remember(asked, chat_id=11, message_id=1)
    await store.remember(answered, chat_id=11, message_id=2)
    await store.settle(answered)

    unsettled = await store.unsettled()

    assert [row.session_id for row in unsettled] == [asked]


@pytest.mark.parametrize("settled", [False, True])
async def test_a_row_survives_a_reopen(tmp_path: Path, settled: bool) -> None:
    """A restart is the case this table exists for, so it is asserted across a real reopen."""
    path = tmp_path / "sessions.sqlite3"
    store = SQLiteTrustNotificationStore(open_database(path))
    session = SessionId.new()
    await store.remember(session, chat_id=11, message_id=42)
    if settled:
        await store.settle(session)

    reopened = SQLiteTrustNotificationStore(open_database(path))
    standing = await reopened.standing_for(session)

    assert standing is not None
    assert standing.settled is settled


async def test_a_session_asked_again_after_being_answered_is_askable_again(tmp_path) -> None:
    """The `settled = 0` in the conflict clause, which nothing else here reaches.

    A session can enter UNTRUSTED, be answered, and enter it again — a later launch into the
    same never-trusted folder. Without the reset, a stale `settled = 1` would suppress a
    genuinely new question forever, and the sibling re-remember test never gets here because
    it re-asks a row that was never settled.
    """
    store = _store(tmp_path)
    session = SessionId.new()
    await store.remember(session, chat_id=11, message_id=42)
    await store.settle(session)

    await store.remember(session, chat_id=11, message_id=99)
    standing = await store.standing_for(session)

    assert standing is not None
    assert standing.settled is False, "a re-asked session must be askable again"
    assert standing.message_id == 99
    assert [row.session_id for row in await store.unsettled()] == [session]
