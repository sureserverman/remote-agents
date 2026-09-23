"""At most one relayed message waits per session, and the newest one is the one that waits.

The owner's ruling (DEC-099): a message for a busy session is queued; a second one replaces the
first, so no stale backlog fires turn after turn. Delivery claims the row -- removing it in the
same transaction -- and a refused delivery puts it back only if nothing newer arrived meanwhile.

The table lives in the UI store (`ui.sqlite3`, DEC-090): it is the bot's own bookkeeping, and a row
written into the watched domain store would wake the watcher and redraw the bot on its own write.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import open_database, open_ui_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS, UI_MIGRATIONS
from remote_agents.adapters.sqlite.queued_prompt_store import SQLiteQueuedPromptStore


@pytest.fixture
def store(tmp_path: Path):
    connection = open_ui_database(tmp_path / "ui.sqlite3")
    try:
        yield SQLiteQueuedPromptStore(connection)
    finally:
        connection.close()


def test_a_second_message_replaces_the_first_and_says_so(store) -> None:
    assert store.queue("s1", "first") is False
    assert store.queue("s1", "second") is True

    pending = store.pending("s1")
    assert pending is not None and pending.text == "second"
    assert store.pending("s2") is None


def test_a_claimed_message_is_delivered_once_and_settled_away(store) -> None:
    store.queue("s1", "hello")

    claimed = store.claim("s1")

    assert claimed is not None and claimed.text == "hello" and claimed.session_id == "s1"
    assert store.claim("s1") is None, "a message in flight cannot be claimed twice"
    assert store.settle(claimed) is True
    assert store.pending("s1") is None


def test_settling_a_claim_that_was_cancelled_meanwhile_says_so(store) -> None:
    store.queue("s1", "hello")
    claimed = store.claim("s1")
    store.cancel("s1")

    assert store.settle(claimed) is False


def test_a_message_cancelled_while_in_flight_does_not_come_back(store) -> None:
    """The owner cancels during a delivery that is then refused: it stays cancelled."""
    store.queue("s1", "hello")
    claimed = store.claim("s1")

    assert store.cancel("s1") is True, "the owner's cancel must reach a message in flight"
    assert store.restore(claimed) is False
    assert store.pending("s1") is None


def test_settling_a_delivery_leaves_a_newer_message_waiting(store) -> None:
    store.queue("s1", "older")
    claimed = store.claim("s1")
    store.queue("s1", "newer")

    store.settle(claimed)

    assert store.pending("s1").text == "newer"


def test_a_claim_abandoned_by_a_crash_can_be_claimed_again(store) -> None:
    from datetime import timedelta

    store.queue("s1", "hello")
    store.claim("s1")

    assert store.claim("s1") is None
    later = datetime.now(UTC) + timedelta(minutes=5)
    reclaimed = store.claim("s1", now=later)
    assert reclaimed is not None and reclaimed.text == "hello"


def test_a_refused_delivery_puts_the_message_back(store) -> None:
    store.queue("s1", "hello")
    claimed = store.claim("s1")

    assert store.restore(claimed) is True
    assert store.pending("s1").text == "hello"


def test_a_restore_yields_to_a_newer_message(store) -> None:
    store.queue("s1", "older")
    claimed = store.claim("s1")
    store.queue("s1", "newer")

    assert store.restore(claimed) is False
    assert store.pending("s1").text == "newer"


def test_cancel_and_clear_remove_the_message(store) -> None:
    store.queue("s1", "a")
    store.queue("s2", "b")

    assert store.cancel("s1") is True
    assert store.cancel("s1") is False
    store.clear("s2")
    assert store.pending("s1") is None and store.pending("s2") is None


def test_the_queued_time_is_recorded(store) -> None:
    before = datetime.now(UTC)
    store.queue("s1", "a")

    assert store.pending("s1").queued_at >= before


def test_the_migration_applies_to_a_store_that_already_has_rows(tmp_path: Path) -> None:
    """A live `ui.sqlite3` is at version 1 with rows in it; version 2 must add, never touch."""
    path = tmp_path / "ui.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_version VALUES (0)")
    connection.commit()
    first = UI_MIGRATIONS[0]
    from remote_agents.adapters.sqlite.migrations import apply_migrations

    apply_migrations(connection, (first,))
    connection.execute("INSERT INTO chat_views VALUES (1, 2, '2026-09-23')")
    connection.commit()
    connection.close()

    reopened = open_ui_database(path)
    try:
        tables = {row[0] for row in reopened.execute("SELECT name FROM sqlite_master")}
        assert "queued_prompts" in tables
        assert reopened.execute("SELECT COUNT(*) FROM chat_views").fetchone()[0] == 1
    finally:
        reopened.close()


def test_the_watched_domain_store_does_not_gain_the_table(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3", migrations=MIGRATIONS)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    finally:
        connection.close()

    assert "queued_prompts" not in tables


def test_a_stale_claimer_cannot_undo_a_later_reclaim(store) -> None:
    """A claimer that stalled past abandonment must not restore or settle the new claim."""
    from datetime import timedelta

    store.queue("s1", "hello")
    stale = store.claim("s1")
    active = store.claim("s1", now=datetime.now(UTC) + timedelta(minutes=5))
    assert active is not None and active.claimed_at != stale.claimed_at

    assert store.restore(stale) is False, "the stale claim un-marked a live delivery"
    store.settle(stale)
    assert store.pending("s1") is not None, "the stale claim settled away a live delivery"
    assert store.claim("s1") is None, "the live delivery must still hold the message"
    store.settle(active)
    assert store.pending("s1") is None
