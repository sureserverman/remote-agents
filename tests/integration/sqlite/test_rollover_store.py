"""Rollovers in the domain store (migration 18): one per handoff, one open per predecessor,
moves only by the domain matrix, an append-only history, and lineage only once COMPLETED."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.adapters.sqlite.database import backup_path, open_database
from remote_agents.adapters.sqlite.migrations import (
    MIGRATIONS,
    UI_MIGRATIONS,
    _statements,
    apply_migrations,
    current_version,
)
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.rollover import (
    NEEDS_SUCCESSOR,
    TERMINAL,
    RolloverState,
    may_stop_predecessor,
)
from remote_agents.ports.rollover_store import IllegalRolloverMove

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
HANDOFF = "h-0123456789abcdef0123"
OTHER_HANDOFF = "h-fedcba9876543210fedc"


def _store(path: Path) -> SQLiteRolloverStore:
    return SQLiteRolloverStore(open_database(path / "sessions.sqlite3"))


async def _open(store: SQLiteRolloverStore, predecessor: SessionId, handoff_id: str = HANDOFF):
    return await store.open_for_ready(
        predecessor,
        handoff_id,
        project_id=ProjectId("remote-agents"),
        profile_id=ProfileId("claude"),
        plan="/abs/plan.md",
        at=NOW,
    )


async def _complete(
    store: SQLiteRolloverStore,
    rollover_id: str,
    successor: SessionId,
    last: RolloverState = RolloverState.COMPLETED,
) -> None:
    at = NOW
    path = (
        RolloverState.SUCCESSOR_STARTING,
        RolloverState.ADOPTING,
        RolloverState.SUCCESSOR_ACCEPTED,
        RolloverState.PREDECESSOR_STOPPING,
        last,
    )
    for state in path[: path.index(last) + 1]:
        at += timedelta(seconds=1)
        successor_id = successor if state is RolloverState.SUCCESSOR_STARTING else None
        await store.advance(rollover_id, state, at=at, successor_session_id=successor_id)


async def test_the_same_handoff_twice_yields_one_row(tmp_path: Path) -> None:
    store, predecessor = _store(tmp_path), SessionId.new()

    first = await _open(store, predecessor)
    second = await _open(store, predecessor)

    assert first is not None and second is not None
    assert second.id == first.id
    assert [r.id for r in await store.open_rollovers()] == [first.id]
    assert first.state is RolloverState.HANDOFF_READY
    assert first.plan == "/abs/plan.md"


async def test_a_handoff_already_finished_is_not_opened_again(tmp_path: Path) -> None:
    store, predecessor = _store(tmp_path), SessionId.new()
    first = await _open(store, predecessor)
    assert first is not None
    await store.advance(first.id, RolloverState.FAILED, at=NOW, failure_code="not-typed")

    again = await _open(store, predecessor)

    assert again is not None and again.id == first.id
    assert again.state is RolloverState.FAILED
    assert await store.open_rollovers() == ()


async def test_a_second_open_rollover_for_one_predecessor_is_refused(tmp_path: Path) -> None:
    store, predecessor = _store(tmp_path), SessionId.new()
    assert await _open(store, predecessor) is not None

    assert await _open(store, predecessor, OTHER_HANDOFF) is None
    assert (
        await store.request(
            predecessor, project_id=ProjectId("p"), profile_id=ProfileId("claude"), at=NOW
        )
        is None
    )
    assert len(await store.open_rollovers()) == 1


async def test_a_finished_rollover_frees_its_predecessor_for_the_next(tmp_path: Path) -> None:
    store, predecessor = _store(tmp_path), SessionId.new()
    first = await _open(store, predecessor)
    assert first is not None
    await store.advance(first.id, RolloverState.CANCELLED, at=NOW)

    second = await _open(store, predecessor, OTHER_HANDOFF)

    assert second is not None and second.id != first.id


async def test_a_ready_advances_the_owners_request_instead_of_opening_another(
    tmp_path: Path,
) -> None:
    store, predecessor = _store(tmp_path), SessionId.new()
    requested = await store.request(
        predecessor, project_id=ProjectId("remote-agents"), profile_id=ProfileId("claude"), at=NOW
    )
    assert requested is not None and requested.state is RolloverState.REQUESTED
    assert requested.handoff_id is None

    ready = await _open(store, predecessor)

    assert ready is not None and ready.id == requested.id
    assert ready.state is RolloverState.HANDOFF_READY
    assert ready.handoff_id == HANDOFF
    assert [(e.from_state, e.to_state) for e in await store.events(ready.id)] == [
        (None, RolloverState.REQUESTED),
        (RolloverState.REQUESTED, RolloverState.HANDOFF_READY),
    ]


async def test_an_illegal_advance_raises_and_writes_no_event(tmp_path: Path) -> None:
    store, predecessor = _store(tmp_path), SessionId.new()
    opened = await _open(store, predecessor)
    assert opened is not None
    before = await store.events(opened.id)

    with pytest.raises(IllegalRolloverMove):
        await store.advance(opened.id, RolloverState.PREDECESSOR_STOPPING, at=NOW)

    assert await store.events(opened.id) == before
    assert (await store.get(opened.id)).state is RolloverState.HANDOFF_READY


async def test_advance_records_successor_and_failure(tmp_path: Path) -> None:
    store, predecessor, successor = _store(tmp_path), SessionId.new(), SessionId.new()
    opened = await _open(store, predecessor)
    assert opened is not None

    starting = await store.advance(
        opened.id, RolloverState.SUCCESSOR_STARTING, at=NOW, successor_session_id=successor
    )
    failed = await store.advance(
        opened.id,
        RolloverState.FAILED,
        at=NOW + timedelta(seconds=1),
        failure_code="successor-untrusted",
        detail="recovery",
    )

    assert starting.successor_session_id == successor
    assert failed.successor_session_id == successor
    assert failed.failure_code == "successor-untrusted"
    assert failed.updated_at == NOW + timedelta(seconds=1)
    assert (await store.events(opened.id))[-1].detail == "recovery"


async def test_an_update_or_delete_on_rollover_events_raises(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")
    store = SQLiteRolloverStore(connection)
    opened = await _open(store, SessionId.new())
    assert opened is not None

    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        with connection:
            connection.execute("UPDATE rollover_events SET to_state = 'completed'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        with connection:
            connection.execute("DELETE FROM rollover_events")
    assert len(await store.events(opened.id)) == 1


async def test_lineage_appears_only_after_completed_and_survives_reopening(
    tmp_path: Path,
) -> None:
    store, predecessor, successor = _store(tmp_path), SessionId.new(), SessionId.new()
    opened = await _open(store, predecessor)
    assert opened is not None
    await store.advance(
        opened.id, RolloverState.SUCCESSOR_STARTING, at=NOW, successor_session_id=successor
    )
    await store.advance(opened.id, RolloverState.ADOPTING, at=NOW)
    await store.advance(opened.id, RolloverState.SUCCESSOR_ACCEPTED, at=NOW)
    await store.advance(opened.id, RolloverState.PREDECESSOR_STOPPING, at=NOW)

    assert await store.continued_as(predecessor) is None
    assert await store.continued_from(successor) is None

    await store.advance(opened.id, RolloverState.COMPLETED, at=NOW)
    reopened = _store(tmp_path)

    assert await reopened.continued_as(predecessor) == successor
    assert await reopened.continued_from(successor) == predecessor
    assert await reopened.continued_as(successor) is None


async def test_a_failed_rollover_with_a_successor_is_not_lineage(tmp_path: Path) -> None:
    store, predecessor, successor = _store(tmp_path), SessionId.new(), SessionId.new()
    opened = await _open(store, predecessor)
    assert opened is not None
    await store.advance(
        opened.id, RolloverState.SUCCESSOR_STARTING, at=NOW, successor_session_id=successor
    )
    await store.advance(opened.id, RolloverState.FAILED, at=NOW, failure_code="not-typed")

    assert await store.continued_as(predecessor) is None
    assert await store.continued_from(successor) is None


async def test_a_chain_of_rollovers_links_each_step(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first, second, third = SessionId.new(), SessionId.new(), SessionId.new()
    one = await _open(store, first)
    assert one is not None
    await _complete(store, one.id, second)
    two = await _open(store, second, OTHER_HANDOFF)
    assert two is not None
    await _complete(store, two.id, third)

    assert await store.continued_from(second) == first
    assert await store.continued_as(second) == third


def test_the_open_index_names_exactly_the_terminal_states(tmp_path: Path) -> None:
    """The partial index spells the terminal states as literals; they must be the domain's."""
    connection = open_database(tmp_path / "sessions.sqlite3")
    (sql,) = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'rollovers_one_open'"
    ).fetchone()
    listed = set(re.findall(r"'([a-z_]+)'", sql))

    assert listed == {state.value for state in TERMINAL}


def test_a_trigger_body_survives_the_migration_runner() -> None:
    """The runner splits a migration into statements; a `;` inside a trigger is not a split."""
    connection = sqlite3.connect(":memory:")
    apply_migrations(
        connection,
        (
            (
                1,
                """
                CREATE TABLE t (x INTEGER);
                CREATE TRIGGER t_no_delete BEFORE DELETE ON t
                BEGIN
                    SELECT RAISE(ABORT, 'kept; whole');
                END;
                INSERT INTO t VALUES (1);
                """,
            ),
        ),
    )

    with pytest.raises(sqlite3.DatabaseError, match="kept; whole"):
        connection.execute("DELETE FROM t")


def test_an_existing_store_migrates_to_18_with_a_backup_and_every_row_kept(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite3"
    before = open_database(path, migrations=tuple(m for m in MIGRATIONS if m[0] < 18))
    before.execute(
        "INSERT INTO sessions(session_id, project_id, profile_id, display_identity, state,"
        " created_at) VALUES ('s1', 'p', 'claude', '{}', 'running', '2026-10-01T00:00:00+00:00')"
    )
    before.commit()
    before.close()

    after = open_database(path)

    assert current_version(after) == 18
    assert after.execute("SELECT COUNT(*) FROM sessions").fetchone() == (1,)
    assert after.execute("SELECT COUNT(*) FROM rollovers").fetchone() == (0,)
    assert current_version(sqlite3.connect(backup_path(path))) == 17


class _BlindOnce(SQLiteRolloverStore):
    """Misses the first handoff lookup, as a writer racing another connection would."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        super().__init__(connection)
        self._blind = True

    def _select(self, where: str, params: tuple):
        if self._blind and where == "handoff_id = ?":
            self._blind = False
            return ()
        return super()._select(where, params)


async def test_a_handoff_taken_by_a_racing_insert_returns_its_row(tmp_path: Path) -> None:
    winner, predecessor = _store(tmp_path), SessionId.new()
    first = await _open(winner, predecessor)
    assert first is not None

    racer = _BlindOnce(open_database(tmp_path / "sessions.sqlite3"))
    again = await _open(racer, predecessor)

    assert again is not None and again.id == first.id
    assert len(await winner.open_rollovers()) == 1


async def test_a_handoff_id_reused_for_another_session_returns_nothing(tmp_path: Path) -> None:
    """An id planted in another checkout must not hand back -- or advance -- someone else's."""
    store = _store(tmp_path)
    first = await _open(store, SessionId.new())
    assert first is not None

    assert await _open(store, SessionId.new()) is None
    racer = _BlindOnce(open_database(tmp_path / "sessions.sqlite3"))
    assert await _open(racer, SessionId.new()) is None
    elsewhere = await store.open_for_ready(
        first.predecessor_session_id,
        HANDOFF,
        project_id=ProjectId("another-project"),
        profile_id=ProfileId("claude"),
        plan=None,
        at=NOW,
    )
    assert elsewhere is None
    assert [r.id for r in await store.open_rollovers()] == [first.id]


async def test_a_handoff_taken_while_advancing_a_request_leaves_the_request(
    tmp_path: Path,
) -> None:
    winner = _store(tmp_path)
    first = await _open(winner, SessionId.new())
    assert first is not None
    asker = SessionId.new()
    requested = await winner.request(
        asker, project_id=ProjectId("remote-agents"), profile_id=ProfileId("claude"), at=NOW
    )
    assert requested is not None

    racer = _BlindOnce(open_database(tmp_path / "sessions.sqlite3"))

    assert await _open(racer, asker) is None
    left = await winner.get(requested.id)
    assert left.state is RolloverState.REQUESTED and left.handoff_id is None
    assert len(await winner.events(requested.id)) == 1


async def test_a_recorded_successor_is_never_replaced(tmp_path: Path) -> None:
    store, successor = _store(tmp_path), SessionId.new()
    opened = await _open(store, SessionId.new())
    assert opened is not None
    await store.advance(
        opened.id, RolloverState.SUCCESSOR_STARTING, at=NOW, successor_session_id=successor
    )

    with pytest.raises(IllegalRolloverMove):
        await store.advance(
            opened.id, RolloverState.ADOPTING, at=NOW, successor_session_id=SessionId.new()
        )

    assert (await store.get(opened.id)).successor_session_id == successor


@pytest.mark.parametrize("migrations", [MIGRATIONS, UI_MIGRATIONS], ids=["domain", "ui"])
def test_every_shipped_migration_splits_as_a_bare_split_did(migrations) -> None:
    """The splitter changed for trigger bodies; every older migration must split as before."""
    for version, sql in migrations:
        if "CREATE TRIGGER" in sql:
            continue
        bare = [part.strip() for part in sql.split(";") if part.strip()]
        assert [s.rstrip(";").strip() for s in _statements(sql)] == bare, version


def test_a_migration_ending_inside_a_statement_is_refused() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        list(_statements("CREATE TABLE t (x TEXT DEFAULT 'a;b)"))


async def test_a_successor_is_recorded_while_starting_as_its_own_history_row(
    tmp_path: Path,
) -> None:
    store, successor = _store(tmp_path), SessionId.new()
    opened = await _open(store, SessionId.new())
    assert opened is not None
    await store.advance(opened.id, RolloverState.SUCCESSOR_STARTING, at=NOW)

    recorded = await store.record_successor(opened.id, successor, at=NOW)
    again = await store.record_successor(opened.id, successor, at=NOW)

    assert recorded.state is RolloverState.SUCCESSOR_STARTING
    assert recorded.successor_session_id == successor == again.successor_session_id
    last = (await store.events(opened.id))[-1]
    assert (last.from_state, last.to_state) == (
        RolloverState.SUCCESSOR_STARTING,
        RolloverState.SUCCESSOR_STARTING,
    )
    assert len(await store.events(opened.id)) == 3
    with pytest.raises(IllegalRolloverMove):
        await store.record_successor(opened.id, SessionId.new(), at=NOW)


async def test_a_successor_is_recorded_only_while_starting_and_never_the_predecessor(
    tmp_path: Path,
) -> None:
    store, predecessor = _store(tmp_path), SessionId.new()
    opened = await _open(store, predecessor)
    assert opened is not None

    with pytest.raises(IllegalRolloverMove):
        await store.record_successor(opened.id, SessionId.new(), at=NOW)
    await store.advance(opened.id, RolloverState.SUCCESSOR_STARTING, at=NOW)
    with pytest.raises(IllegalRolloverMove):
        await store.record_successor(opened.id, predecessor, at=NOW)
    assert (await store.get(opened.id)).successor_session_id is None


async def test_no_move_reaches_a_successor_state_without_a_recorded_successor(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    opened = await _open(store, SessionId.new())
    assert opened is not None
    await store.advance(opened.id, RolloverState.SUCCESSOR_STARTING, at=NOW)

    with pytest.raises(IllegalRolloverMove):
        await store.advance(opened.id, RolloverState.ADOPTING, at=NOW)

    assert (await store.get(opened.id)).state is RolloverState.SUCCESSOR_STARTING


@pytest.mark.parametrize("state", sorted(NEEDS_SUCCESSOR))
def test_the_schema_refuses_a_successor_state_without_a_successor(
    tmp_path: Path, state: RolloverState
) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")
    with pytest.raises(sqlite3.IntegrityError):
        _raw_row(connection, state, successor=None)


@pytest.mark.parametrize("state", list(RolloverState))
def test_the_schema_admits_every_domain_state_and_no_other(
    tmp_path: Path, state: RolloverState
) -> None:
    """A state added to the domain without migration 18's CHECK would be unwritable."""
    connection = open_database(tmp_path / "sessions.sqlite3")
    _raw_row(connection, state, successor=str(SessionId.new()))
    with pytest.raises(sqlite3.IntegrityError):
        _raw_row(connection, "bogus", successor=str(SessionId.new()))


def test_the_schema_refuses_a_session_succeeding_itself(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")
    same = str(SessionId.new())
    with pytest.raises(sqlite3.IntegrityError):
        _raw_row(connection, RolloverState.SUCCESSOR_STARTING, successor=same, predecessor=same)


def _raw_row(connection, state, *, successor, predecessor=None) -> None:
    with connection:
        connection.execute(
            "INSERT INTO rollovers(rollover_id, predecessor_session_id, successor_session_id,"
            " project_id, profile_id, reason, state, requested_at, updated_at)"
            " VALUES (?, ?, ?, 'p', 'claude', 'workflow', ?, 't', 't')",
            (
                SessionId.new().value.hex,
                predecessor or str(SessionId.new()),
                successor,
                getattr(state, "value", state),
            ),
        )


@pytest.mark.parametrize("ending", [RolloverState.FAILED, RolloverState.STOP_FAILED])
async def test_after_a_failed_rollover_a_ready_waits_for_the_owner(
    tmp_path: Path, ending: RolloverState
) -> None:
    """A forged `ready` costs one extra session, never a stream of them (DEC-115)."""
    store, predecessor = _store(tmp_path), SessionId.new()
    first = await _open(store, predecessor)
    assert first is not None
    if ending is RolloverState.FAILED:
        await store.advance(first.id, ending, at=NOW, failure_code="not-typed")
    else:
        await _complete(store, first.id, SessionId.new(), last=ending)

    assert await _open(store, predecessor, OTHER_HANDOFF) is None

    asked = await store.request(
        predecessor, project_id=ProjectId("remote-agents"), profile_id=ProfileId("claude"), at=NOW
    )
    assert asked is not None
    ready = await _open(store, predecessor, OTHER_HANDOFF)
    assert ready is not None and ready.id == asked.id


async def test_a_cancelled_rollover_does_not_hold_the_next(tmp_path: Path) -> None:
    store, predecessor = _store(tmp_path), SessionId.new()
    first = await _open(store, predecessor)
    assert first is not None
    await store.advance(first.id, RolloverState.CANCELLED, at=NOW)

    assert await _open(store, predecessor, OTHER_HANDOFF) is not None


async def test_a_failure_code_is_kept_on_its_history_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    opened = await _open(store, SessionId.new())
    assert opened is not None

    await store.advance(opened.id, RolloverState.FAILED, at=NOW, failure_code="not-typed")

    assert (await store.events(opened.id))[-1].failure_code == "not-typed"


async def test_a_row_without_a_successor_licenses_no_stop(tmp_path: Path) -> None:
    store, successor = _store(tmp_path), SessionId.new()
    opened = await _open(store, SessionId.new())
    assert opened is not None
    await _complete(store, opened.id, successor, last=RolloverState.SUCCESSOR_ACCEPTED)
    accepted = await store.get(opened.id)

    assert may_stop_predecessor(accepted) is True
    assert may_stop_predecessor(replace(accepted, successor_session_id=None)) is False


async def test_a_request_is_recorded_once_and_only_while_requested(tmp_path: Path) -> None:
    store, predecessor = _store(tmp_path), SessionId.new()
    requested = await store.request(
        predecessor, project_id=ProjectId("remote-agents"), profile_id=ProfileId("claude"), at=NOW
    )
    assert requested is not None

    assert await store.record_request(requested.id, at=NOW) is True
    assert await store.record_request(requested.id, at=NOW) is False
    reopened = _store(tmp_path)
    assert await reopened.record_request(requested.id, at=NOW) is False
    assert [e.detail for e in await store.events(requested.id)].count("request written") == 1

    ready = await _open(store, SessionId.new())
    assert ready is not None
    assert await store.record_request(ready.id, at=NOW) is False
    assert await store.record_request("no-such-rollover", at=NOW) is False


async def test_a_note_is_its_own_history_row_and_moves_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    opened = await _open(store, SessionId.new())
    assert opened is not None

    await store.note(opened.id, "restart: launch", at=NOW + timedelta(minutes=1))

    last = (await store.events(opened.id))[-1]
    assert (last.from_state, last.to_state, last.detail) == (
        RolloverState.HANDOFF_READY,
        RolloverState.HANDOFF_READY,
        "restart: launch",
    )
    assert (await store.get(opened.id)).state is RolloverState.HANDOFF_READY
    with pytest.raises(LookupError):
        await store.note("no-such-rollover", "x", at=NOW)


async def test_an_owners_cancelled_request_does_not_release_the_hold(tmp_path: Path) -> None:
    """Asking and then withdrawing is not asking: the failure still holds the next `ready`."""
    store, predecessor = _store(tmp_path), SessionId.new()
    first = await _open(store, predecessor)
    assert first is not None
    await store.advance(first.id, RolloverState.FAILED, at=NOW, failure_code="not-typed")
    asked = await store.request(
        predecessor, project_id=ProjectId("remote-agents"), profile_id=ProfileId("claude"), at=NOW
    )
    assert asked is not None
    await store.advance(asked.id, RolloverState.CANCELLED, at=NOW + timedelta(seconds=1))

    assert await _open(store, predecessor, OTHER_HANDOFF) is None
