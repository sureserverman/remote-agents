"""Schedules in the domain store (migration 17): round-trip, `due`, `record_fire`, migration."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS, UI_TABLES, current_version
from remote_agents.adapters.sqlite.schedule_store import SQLiteScheduleStore
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.schedules import Once, Repeat, Schedule, Weekday

NOW = datetime(2026, 10, 2, 7, 0, tzinfo=UTC)


def _schedule(schedule_id: str, *, when=None, next_fire_at=NOW, paused=False) -> Schedule:
    return Schedule(
        id=schedule_id,
        project_id=ProjectId("remote-agents"),
        profile_id=ProfileId("claude"),
        prompt="reply with OK\nand nothing else",
        when=when if when is not None else Repeat.weekdays(time(9, 0)),
        paused=paused,
        next_fire_at=next_fire_at,
        created_at=NOW - timedelta(days=1),
    )


async def test_both_kinds_of_when_round_trip(tmp_path: Path) -> None:
    store = SQLiteScheduleStore(open_database(tmp_path / "sessions.sqlite3"))
    repeat = _schedule("a1", when=Repeat(frozenset({Weekday.MON, Weekday.THU}), time(3, 15)))
    once = _schedule("b2", when=Once(datetime(2026, 10, 3, 3, 0)))
    await store.add(repeat)
    await store.add(once)

    assert await store.get("a1") == repeat
    assert await store.get("b2") == once
    assert await store.get("missing") is None
    assert {schedule.id for schedule in await store.list()} == {"a1", "b2"}


async def test_due_excludes_paused_and_future_rows_oldest_first(tmp_path: Path) -> None:
    store = SQLiteScheduleStore(open_database(tmp_path / "sessions.sqlite3"))
    await store.add(_schedule("late", next_fire_at=NOW - timedelta(minutes=5)))
    await store.add(_schedule("later", next_fire_at=NOW - timedelta(minutes=1)))
    await store.add(_schedule("exact", next_fire_at=NOW))
    await store.add(_schedule("future", next_fire_at=NOW + timedelta(seconds=1)))
    await store.add(_schedule("paused", next_fire_at=NOW - timedelta(hours=1), paused=True))

    assert [schedule.id for schedule in await store.due(NOW)] == ["late", "later", "exact"]


async def test_pausing_drops_a_row_from_due_and_resuming_sets_its_next_time(
    tmp_path: Path,
) -> None:
    store = SQLiteScheduleStore(open_database(tmp_path / "sessions.sqlite3"))
    await store.add(_schedule("a1", next_fire_at=NOW - timedelta(minutes=1)))

    paused = await store.set_paused("a1", True, next_fire_at=None)
    assert paused is not None and paused.paused
    assert await store.due(NOW) == ()

    resumed = await store.set_paused("a1", False, next_fire_at=NOW + timedelta(days=1))
    assert resumed is not None and not resumed.paused
    assert resumed.next_fire_at == NOW + timedelta(days=1)
    assert await store.set_paused("missing", True, next_fire_at=None) is None


async def test_record_fire_advances_a_repeat_and_deletes_a_finished_one_shot(
    tmp_path: Path,
) -> None:
    store = SQLiteScheduleStore(open_database(tmp_path / "sessions.sqlite3"))
    await store.add(_schedule("rep"))
    await store.add(_schedule("one", when=Once(datetime(2026, 10, 2, 9, 0))))

    await store.record_fire("rep", NOW, "sess-1", NOW + timedelta(days=1))
    advanced = await store.get("rep")
    assert advanced is not None
    assert advanced.next_fire_at == NOW + timedelta(days=1)
    assert advanced.last_fire_at == NOW
    assert advanced.last_session_id == "sess-1"

    await store.record_fire("one", NOW, None, None)
    assert await store.get("one") is None


async def test_a_skipped_fire_keeps_the_previous_session(tmp_path: Path) -> None:
    store = SQLiteScheduleStore(open_database(tmp_path / "sessions.sqlite3"))
    await store.add(_schedule("rep"))
    await store.record_fire("rep", NOW, "sess-1", NOW + timedelta(days=1))
    await store.record_fire("rep", NOW + timedelta(days=1), None, NOW + timedelta(days=2))

    kept = await store.get("rep")
    assert kept is not None and kept.last_session_id == "sess-1"


async def test_delete_removes_the_row(tmp_path: Path) -> None:
    store = SQLiteScheduleStore(open_database(tmp_path / "sessions.sqlite3"))
    await store.add(_schedule("a1"))

    assert await store.delete("a1") is True
    assert await store.get("a1") is None
    assert await store.delete("a1") is False


def test_schedules_is_a_domain_table_and_not_a_ui_one() -> None:
    assert "schedules" not in UI_TABLES
    assert any("CREATE TABLE schedules" in sql for _, sql in MIGRATIONS)


def test_migrating_a_current_store_adds_the_table_and_keeps_every_other_row(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite3"
    before = open_database(path, migrations=MIGRATIONS[:-1])
    before.execute(
        "INSERT INTO sessions(session_id, project_id, profile_id, display_identity, state,"
        " created_at) VALUES ('s1', 'p', 'claude', '{}', 'running', '2026-10-01T00:00:00+00:00')"
    )
    before.execute("INSERT INTO idempotency_claims VALUES ('k1', '2026-10-01T00:00:00+00:00')")
    before.commit()
    tables = [
        row[0]
        for row in before.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name <> 'schema_version'"
        )
    ]
    counts = {
        table: before.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables
    }
    before.close()
    assert "schedules" not in tables

    after = open_database(path)

    assert current_version(after) == len(MIGRATIONS)
    names = {row[0] for row in after.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "schedules" in names
    for table, count in counts.items():
        assert after.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count, table
    assert counts["sessions"] == 1 and counts["idempotency_claims"] == 1
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 0
