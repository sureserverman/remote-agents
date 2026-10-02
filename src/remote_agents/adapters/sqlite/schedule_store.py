"""Schedules in the domain store (migration 17), so a change wakes the store watcher (DEC-090)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, time

from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.schedules import Once, Repeat, Schedule, Weekday, When

_COLUMNS = (
    "schedule_id, project_id, profile_id, prompt, once_at, repeat_days, repeat_time, paused,"
    " next_fire_at, last_fire_at, last_session_id, created_at"
)


class SQLiteScheduleStore:
    """`ScheduleStore` over the domain database the session store writes."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    async def add(self, schedule: Schedule) -> None:
        once_at, repeat_days, repeat_time = _when_columns(schedule.when)
        with self._connection:
            self._connection.execute(
                f"INSERT INTO schedules({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    schedule.id,
                    str(schedule.project_id),
                    str(schedule.profile_id),
                    schedule.prompt,
                    once_at,
                    repeat_days,
                    repeat_time,
                    int(schedule.paused),
                    _stored(schedule.next_fire_at),
                    _stored(schedule.last_fire_at),
                    schedule.last_session_id,
                    _stored(schedule.created_at),
                ),
            )

    async def get(self, schedule_id: str) -> Schedule | None:
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM schedules WHERE schedule_id = ?", (schedule_id,)
        ).fetchone()
        return None if row is None else _schedule(row)

    async def list(self) -> tuple[Schedule, ...]:
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM schedules ORDER BY created_at, schedule_id"
        ).fetchall()
        return tuple(_schedule(row) for row in rows)

    async def set_paused(
        self, schedule_id: str, paused: bool, *, next_fire_at: datetime | None
    ) -> Schedule | None:
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE schedules SET paused = ?, next_fire_at = ? WHERE schedule_id = ?",
                (int(paused), _stored(next_fire_at), schedule_id),
            )
        return None if cursor.rowcount == 0 else await self.get(schedule_id)

    async def delete(self, schedule_id: str) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM schedules WHERE schedule_id = ?", (schedule_id,)
            )
        return cursor.rowcount > 0

    async def due(self, now: datetime) -> tuple[Schedule, ...]:
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM schedules"
            " WHERE paused = 0 AND next_fire_at IS NOT NULL AND next_fire_at <= ?"
            " ORDER BY next_fire_at, schedule_id",
            (_stored(now),),
        ).fetchall()
        return tuple(_schedule(row) for row in rows)

    async def record_fire(
        self,
        schedule_id: str,
        fired_at: datetime,
        session_id: str | None,
        next_fire_at: datetime | None,
    ) -> None:
        with self._connection:
            if next_fire_at is None:
                self._connection.execute(
                    "DELETE FROM schedules WHERE schedule_id = ?", (schedule_id,)
                )
                return
            self._connection.execute(
                "UPDATE schedules SET next_fire_at = ?, last_fire_at = ?,"
                " last_session_id = COALESCE(?, last_session_id) WHERE schedule_id = ?",
                (_stored(next_fire_at), _stored(fired_at), session_id, schedule_id),
            )


def _when_columns(when: When) -> tuple[str | None, str | None, str | None]:
    if isinstance(when, Once):
        return when.at.strftime("%Y-%m-%dT%H:%M"), None, None
    days = ",".join(str(int(day)) for day in sorted(when.days))
    return None, days, when.time.strftime("%H:%M")


def _when(once_at: str | None, repeat_days: str | None, repeat_time: str | None) -> When:
    if once_at is not None:
        return Once(datetime.fromisoformat(once_at))
    assert repeat_days is not None and repeat_time is not None
    days = frozenset(Weekday(int(day)) for day in repeat_days.split(","))
    return Repeat(days, time.fromisoformat(repeat_time))


def _schedule(row: tuple) -> Schedule:
    (
        schedule_id,
        project_id,
        profile_id,
        prompt,
        once_at,
        repeat_days,
        repeat_time,
        paused,
        next_fire_at,
        last_fire_at,
        last_session_id,
        created_at,
    ) = row
    return Schedule(
        id=schedule_id,
        project_id=ProjectId(project_id),
        profile_id=ProfileId(profile_id),
        prompt=prompt,
        when=_when(once_at, repeat_days, repeat_time),
        paused=bool(paused),
        next_fire_at=_instant(next_fire_at),
        created_at=_instant(created_at) or datetime.fromtimestamp(0, UTC),
        last_fire_at=_instant(last_fire_at),
        last_session_id=last_session_id,
    )


def _stored(value: datetime | None) -> str | None:
    """Fixed-width UTC text, so `due` can compare instants as strings."""
    if value is None:
        return None
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _instant(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)
