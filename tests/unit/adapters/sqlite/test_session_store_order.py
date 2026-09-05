"""`SessionStore.list` returns insertion order because it says so, not because it happened to.

Every reader of this list has been seeing insertion order since the table existed, and none of
them was promised it: the query was a bare `SELECT` and the row order was whatever the query
planner chose. That is a contract by coincidence, and the cost of it landing wrong is not an
abstract one. The TUI restores its highlight by session id rather than by index (DEC-052,
DEC-062, `adapters/tui/screens/base.py`), so a reorder does not move the *session* the cursor
is on -- it moves the *line* that session sits on. To the owner that reads as the wrong row
lighting up, on a ten-second refresh nobody pressed, with `s` and `c` bound underneath it.

These tests are deliberately of two kinds, because the behavioural one alone cannot fail on a
table this shape. A full scan of a rowid table yields rowid order, so `SELECT` with no clause
and `SELECT ... ORDER BY rowid` agree for as long as the planner keeps choosing a full scan --
which is exactly why nobody noticed the guarantee was missing. The second and third tests
therefore attack the coincidence directly: one puts the planner in a position where it picks a
different scan, the other reads the statement that was actually executed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


def _session(
    session_id: SessionId, sequence: int, state: SessionState = SessionState.RUNNING
) -> SessionRecord:
    """One saveable projection, distinguished only by the two fields these tests read."""
    return SessionRecord(
        session_id,
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", sequence),
        state,
        datetime.now(UTC),
    )


async def test_sessions_come_back_in_the_order_they_were_saved(tmp_path: Path) -> None:
    """The stated contract, at its plainest: saved first, listed first, growing at the tail."""
    store = SQLiteSessionStore(open_database(tmp_path / "sessions.sqlite3"))
    first, second, third = SessionId.new(), SessionId.new(), SessionId.new()
    for sequence, session_id in enumerate((first, second, third), start=1):
        await store.save(_session(session_id, sequence))

    listed = await store.list()

    assert [record.session_id for record in listed] == [first, second, third]


async def test_the_order_holds_when_the_planner_would_have_grouped_by_state(
    tmp_path: Path,
) -> None:
    """The test that can actually fail, by taking the coincidence away.

    A bare `SELECT` over a rowid table with no usable index is a full scan, and a full scan
    emits rowid order -- so on today's schema the unordered query and the ordered one agree,
    and the assertion above passes either way. The agreement is a property of the plan, not of
    the SQL, and the plan is not ours to fix: an index added by a later migration, or a
    version of SQLite that costs the same choice differently, changes it without touching this
    adapter.

    Adding an index on `state` here reproduces that future locally. With one present, the
    `state IN (?, ?)` branch is served as a search per value in sorted value order -- every
    `running` row, then every `starting` row -- which is emphatically not the order they were
    saved in. Insertion order surviving that is the guarantee; without `ORDER BY rowid` this
    comes back as first, third, second.
    """
    connection = open_database(tmp_path / "sessions.sqlite3")
    connection.execute("CREATE INDEX sessions_state ON sessions(state)")
    store = SQLiteSessionStore(connection)
    first, second, third = SessionId.new(), SessionId.new(), SessionId.new()
    await store.save(_session(first, 1, SessionState.RUNNING))
    await store.save(_session(second, 2, SessionState.STARTING))
    await store.save(_session(third, 3, SessionState.RUNNING))

    listed = await store.list({SessionState.RUNNING, SessionState.STARTING})

    assert [record.session_id for record in listed] == [first, second, third]


async def test_the_executed_statement_orders_after_it_filters(tmp_path: Path) -> None:
    """Read the SQL that ran, because the filtered branch is where the clause can go wrong.

    `list` builds its statement in two pieces and appends the `WHERE` for the filtered call.
    An `ORDER BY` written into the base string rather than after that append is not a subtler
    version of the same fix -- it is a syntax error SQLite raises at execute time, and only on
    the filtered branch, which is the branch the dashboard uses and the plainest test above
    does not. So both branches are executed and both executed statements are inspected.
    """
    connection = open_database(tmp_path / "sessions.sqlite3")
    store = SQLiteSessionStore(connection)
    await store.save(_session(SessionId.new(), 1))
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        await store.list()
        await store.list({SessionState.RUNNING})
    finally:
        connection.set_trace_callback(None)

    selects = [statement for statement in statements if statement.lstrip().startswith("SELECT")]
    assert len(selects) == 2, "one statement per list call, or this is inspecting the wrong SQL"
    for statement in selects:
        assert "ORDER BY rowid" in statement
    filtered = selects[1]
    assert filtered.index("WHERE") < filtered.index("ORDER BY rowid"), (
        "an ORDER BY ahead of the WHERE is a syntax error on exactly the branch the dashboard uses"
    )
