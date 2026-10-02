"""An in-memory `ScheduleStore`, for tests that need schedules without a database."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from remote_agents.ports.schedules import Schedule


class MemoryScheduleStore:
    """`ScheduleStore` over a dict, with the SQLite store's ordering and deletion rules."""

    def __init__(self, *schedules: Schedule) -> None:
        self.rows: dict[str, Schedule] = {schedule.id: schedule for schedule in schedules}

    async def add(self, schedule: Schedule) -> None:
        self.rows[schedule.id] = schedule

    async def get(self, schedule_id: str) -> Schedule | None:
        return self.rows.get(schedule_id)

    async def list(self) -> tuple[Schedule, ...]:
        return tuple(sorted(self.rows.values(), key=lambda row: (row.created_at, row.id)))

    async def set_paused(
        self, schedule_id: str, paused: bool, *, next_fire_at: datetime | None
    ) -> Schedule | None:
        if not paused and next_fire_at is None:
            raise ValueError("an unpaused schedule needs its next fire")
        schedule = self.rows.get(schedule_id)
        if schedule is None:
            return None
        schedule = replace(schedule, paused=paused, next_fire_at=next_fire_at)
        self.rows[schedule_id] = schedule
        return schedule

    async def delete(self, schedule_id: str) -> bool:
        return self.rows.pop(schedule_id, None) is not None

    async def due(self, now: datetime) -> tuple[Schedule, ...]:
        due = [
            schedule
            for schedule in self.rows.values()
            if not schedule.paused
            and schedule.next_fire_at is not None
            and schedule.next_fire_at <= now
        ]
        return tuple(sorted(due, key=lambda schedule: (schedule.next_fire_at, schedule.id)))

    async def record_fire(
        self,
        schedule_id: str,
        fired_at: datetime,
        session_id: str | None,
        next_fire_at: datetime | None,
    ) -> None:
        schedule = self.rows.get(schedule_id)
        if schedule is None:
            return
        if next_fire_at is None:
            del self.rows[schedule_id]
            return
        self.rows[schedule_id] = replace(
            schedule,
            next_fire_at=None if schedule.paused else next_fire_at,
            last_fire_at=fired_at,
            last_session_id=session_id if session_id is not None else schedule.last_session_id,
        )
