"""The second store: where a surface's own bookkeeping lives, beside the domain one.

**Why there are two files at all.** `StoreWatch` fingerprints the database it is given, and
publishes a change when the bytes move. With one file, the bot minting a keyboard and another
process launching a session were the same event to the watcher — so an open sessions page
redrew, minted, published, and redrew again, thirty edits a minute into a chat that Telegram
then flood-banned for six hours. The signal could not mean "a session changed" while the file
also carried what the surface writes about itself.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import (
    open_database,
    open_ui_database,
    ui_database_path,
)
from remote_agents.adapters.sqlite.migrations import MIGRATIONS, UI_MIGRATIONS, UI_TABLES


def test_the_ui_store_sits_beside_the_domain_store() -> None:
    """One directory, two files. The layout is this module's to decide (see `watched_paths`)."""
    assert ui_database_path(Path("/state/sessions.sqlite3")) == Path("/state/ui.sqlite3")


def test_opening_a_fresh_ui_store_creates_every_table_in_the_moved_set(tmp_path: Path) -> None:
    """Swept over `UI_TABLES`, not spot-checked on one table.

    `UI_TABLES` is the set both the migration and the split verifier read, so a table added to
    one and forgotten in the other fails here rather than at a gate.
    """
    connection = open_ui_database(tmp_path / "ui.sqlite3")
    try:
        present = {
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()

    assert UI_TABLES, "the moved set is empty, so this test would pass having checked nothing"
    assert set(UI_TABLES) <= present, f"missing: {sorted(set(UI_TABLES) - present)}"


def test_the_ui_store_carries_no_domain_table(tmp_path: Path) -> None:
    """The other direction of the same claim: the split is only worth anything if it is clean."""
    connection = open_ui_database(tmp_path / "ui.sqlite3")
    try:
        present = {
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()

    assert not present & {"sessions", "session_events", "agent_activity"}


def test_the_two_stores_version_independently(tmp_path: Path) -> None:
    """Two migration lists, two counters.

    A shared counter would make the next domain migration claim to have run against the UI
    store, which is how a schema check starts lying about a file nobody migrated.
    """
    domain = open_database(tmp_path / "sessions.sqlite3")
    ui = open_ui_database(tmp_path / "ui.sqlite3")
    try:
        domain_version = domain.execute("SELECT version FROM schema_version").fetchone()[0]
        ui_version = ui.execute("SELECT version FROM schema_version").fetchone()[0]
    finally:
        domain.close()
        ui.close()

    # Migrating one store must leave the other's counter alone. The first assertion pins the
    # UI store's own version; this one pins that the domain store did not follow it, which is
    # the regression a shared counter would actually produce.
    assert ui_version == len(UI_MIGRATIONS)
    assert domain_version == len(MIGRATIONS)
    assert ui_version != domain_version


def test_opening_the_ui_store_twice_changes_nothing(tmp_path: Path) -> None:
    """Idempotence, because `open_database` runs on every process start."""
    path = tmp_path / "ui.sqlite3"
    first = open_ui_database(path)
    try:
        before = (
            first.execute("SELECT version FROM schema_version").fetchone()[0],
            sorted(
                name
                for (name,) in first.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            ),
        )
    finally:
        first.close()

    second = open_ui_database(path)
    try:
        after = (
            second.execute("SELECT version FROM schema_version").fetchone()[0],
            sorted(
                name
                for (name,) in second.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            ),
        )
    finally:
        second.close()

    assert before == after


@pytest.mark.parametrize("table", ["callback_states", "chat_views"])
def test_a_moved_table_is_writable_in_its_new_home(tmp_path: Path, table: str) -> None:
    """Created, not merely named: a CREATE that never ran leaves the same empty `sqlite_master`
    absence as a table nobody declared."""
    connection = open_ui_database(tmp_path / "ui.sqlite3")
    try:
        count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    finally:
        connection.close()
    assert count == 0


def test_the_ui_store_is_owner_only(tmp_path: Path) -> None:
    """It holds live callback tokens, the owner's user id and their chat id.

    The domain store is narrowed to 0600 deliberately by `ProductionPaths.open_database`; this
    one is opened directly and so inherited the process umask instead — measured at 0644 on a
    drilled copy, world-readable, with the tokens in it. Asserted rather than assumed, because
    a mode set once and never checked is a mode a later refactor silently drops.
    """
    path = tmp_path / "ui.sqlite3"
    open_ui_database(path).close()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_ui_store_the_split_creates_is_owner_only(tmp_path: Path) -> None:
    """The *other* creation path, which is the one a real host takes on upgrade.

    `split_stores` opens the UI store itself the first time it copies. A permission fix that
    covered only the direct call would leave every upgraded host world-readable.
    """
    from remote_agents.adapters.sqlite.migrations import MIGRATIONS
    from remote_agents.adapters.sqlite.store_split import split_stores

    domain = tmp_path / "sessions.sqlite3"
    pre_split = [entry for entry in MIGRATIONS if entry[0] < 14]
    connection = open_database(domain, migrations=pre_split)
    try:
        connection.execute(
            "INSERT INTO callback_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("c1_tok", "session.detail", "e1", 7, 11, 100, 0, 0, "2026-09-14T00:00:00+00:00"),
        )
        connection.commit()
    finally:
        connection.close()

    split_stores(domain)

    assert stat.S_IMODE(ui_database_path(domain).stat().st_mode) == 0o600
