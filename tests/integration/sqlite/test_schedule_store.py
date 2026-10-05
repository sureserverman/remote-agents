"""Schedules in the domain store (migration 17): round-trip, `due`, `record_fire`, migration."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

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
    # By version, not by position: `MIGRATIONS[:-1]` stopped meaning "before schedules" when
    # migration 18 was appended, as test_limit_hit_storage records for its own columns.
    before = open_database(path, migrations=tuple(m for m in MIGRATIONS if m[0] < 17))
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


async def test_due_orders_instants_given_in_any_zone_and_with_microseconds(
    tmp_path: Path,
) -> None:
    """Text order must be time order whatever zone or precision an instant arrives in."""
    store = SQLiteScheduleStore(open_database(tmp_path / "sessions.sqlite3"))
    tokyo, los_angeles = timezone(timedelta(hours=9)), timezone(timedelta(hours=-8))
    # Earliest first in time, but lexically last if the offset were kept.
    await store.add(_schedule("east", next_fire_at=datetime(2026, 10, 2, 15, 0, tzinfo=tokyo)))
    await store.add(
        _schedule("west", next_fire_at=datetime(2026, 10, 1, 23, 0, 1, 500, tzinfo=los_angeles))
    )
    await store.add(_schedule("micro", next_fire_at=NOW.replace(microsecond=1)))

    # 06:00Z, 07:00:01.0005Z; "micro" is 07:00:00.000001Z, after NOW.
    assert [schedule.id for schedule in await store.due(NOW)] == ["east"]
    later = datetime(2026, 10, 2, 0, 0, 2, tzinfo=los_angeles)  # 08:00:02Z
    assert [schedule.id for schedule in await store.due(later)] == ["east", "micro", "west"]


def test_instants_are_stored_as_fixed_width_utc_text(tmp_path: Path) -> None:
    import asyncio

    connection = open_database(tmp_path / "sessions.sqlite3")
    store = SQLiteScheduleStore(connection)
    asyncio.run(store.add(_schedule("a1", next_fire_at=datetime(2026, 10, 2, 9, 0, tzinfo=UTC))))
    stored = connection.execute("SELECT next_fire_at, created_at FROM schedules").fetchone()
    shape = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00$")
    assert all(shape.match(value) for value in stored), stored


async def test_a_naive_instant_is_refused(tmp_path: Path) -> None:
    store = SQLiteScheduleStore(open_database(tmp_path / "sessions.sqlite3"))
    with pytest.raises(ValueError, match="zone"):
        await store.add(_schedule("a1", next_fire_at=datetime(2026, 10, 2, 9, 0)))


@pytest.mark.parametrize(
    ("once_at", "repeat_days", "repeat_time"),
    [
        ("2026-10-02T09:00", "1", "09:00"),
        (None, None, None),
        (None, "1", None),
        (None, None, "09:00"),
        (None, "", "09:00"),
        ("2026-10-02T09:00", None, "09:00"),
    ],
    ids=["both", "neither", "days-only", "time-only", "empty-days", "once-and-time"],
)
def test_the_table_refuses_a_row_that_is_not_one_kind_of_when(
    tmp_path: Path, once_at, repeat_days, repeat_time
) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO schedules(schedule_id, project_id, profile_id, prompt, once_at,"
            " repeat_days, repeat_time, created_at) VALUES ('x', 'p', 'claude', 'hi', ?, ?, ?,"
            " '2026-10-02T07:00:00.000000+00:00')",
            (once_at, repeat_days, repeat_time),
        )


@pytest.mark.parametrize(
    ("once_at", "repeat_days", "repeat_time"),
    [("2026-10-02T09:00", None, None), (None, "0,6", "09:00")],
    ids=["once", "repeat"],
)
def test_the_table_accepts_each_kind_of_when(
    tmp_path: Path, once_at, repeat_days, repeat_time
) -> None:
    connection = open_database(tmp_path / "sessions.sqlite3")
    connection.execute(
        "INSERT INTO schedules(schedule_id, project_id, profile_id, prompt, once_at,"
        " repeat_days, repeat_time, created_at) VALUES ('x', 'p', 'claude', 'hi', ?, ?, ?,"
        " '2026-10-02T07:00:00.000000+00:00')",
        (once_at, repeat_days, repeat_time),
    )


async def test_a_fire_recorded_on_a_schedule_paused_meanwhile_leaves_it_without_a_time(
    tmp_path: Path,
) -> None:
    store = SQLiteScheduleStore(open_database(tmp_path / "sessions.sqlite3"))
    await store.add(_schedule("rep"))
    await store.set_paused("rep", True, next_fire_at=None)

    await store.record_fire("rep", NOW, "sess-1", NOW + timedelta(days=1))

    kept = await store.get("rep")
    assert kept is not None and kept.paused and kept.next_fire_at is None
    assert kept.last_session_id == "sess-1"


async def test_an_unpaused_schedule_without_a_next_time_is_refused(tmp_path: Path) -> None:
    store = SQLiteScheduleStore(open_database(tmp_path / "sessions.sqlite3"))
    await store.add(_schedule("rep"))
    with pytest.raises(ValueError, match="next fire"):
        await store.set_paused("rep", False, next_fire_at=None)
