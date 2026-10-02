"""A scheduled session: which agent to start in which project, when, and what to type into it.

**When is the host's local wall clock**, never an instant: a schedule for 09:00 means 09:00 on
the clock on the wall, before and after a DST change. So `Once` holds a naive local datetime and
`Repeat` a naive time of day, and only `next_fire_at` -- computed from them by
`application/schedule_times.next_fire` -- is an instant, in UTC.

No cron syntax. A repeat is a set of weekdays and one time; daily and weekdays are constructors
over that set, not kinds of their own, so the store and every surface handle one shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import IntEnum
from typing import Protocol

from remote_agents.domain.models import ProfileId, ProjectId


class Weekday(IntEnum):
    """Monday is 0, as `datetime.weekday()` numbers it."""

    MON = 0
    TUE = 1
    WED = 2
    THU = 3
    FRI = 4
    SAT = 5
    SUN = 6


@dataclass(frozen=True, slots=True)
class Once:
    """One local date and time. Seconds are dropped: a schedule is set to the minute."""

    at: datetime

    def __post_init__(self) -> None:
        if self.at.tzinfo is not None:
            raise ValueError("a one-shot is set in local wall time, without a zone")
        object.__setattr__(self, "at", self.at.replace(second=0, microsecond=0))


@dataclass(frozen=True, slots=True)
class Repeat:
    """A local time of day on each of a set of weekdays."""

    days: frozenset[Weekday]
    time: time

    def __post_init__(self) -> None:
        if not self.days:
            raise ValueError("a repeat needs at least one day")
        if self.time.tzinfo is not None:
            raise ValueError("a repeat is set in local wall time, without a zone")
        object.__setattr__(self, "days", frozenset(Weekday(day) for day in self.days))
        object.__setattr__(self, "time", self.time.replace(second=0, microsecond=0))

    @classmethod
    def daily(cls, at: time) -> Repeat:
        return cls(frozenset(Weekday), at)

    @classmethod
    def weekdays(cls, at: time) -> Repeat:
        return cls(frozenset(day for day in Weekday if day <= Weekday.FRI), at)


When = Once | Repeat


@dataclass(frozen=True, slots=True)
class Schedule:
    """One schedule as stored. `prompt` is the owner's text as typed; it is escaped by each
    surface that shows it (DEC-014) and cleaned by the send that types it."""

    id: str
    project_id: ProjectId
    profile_id: ProfileId
    prompt: str
    when: When
    paused: bool
    next_fire_at: datetime | None
    """The next fire, in UTC. None only while paused: a resume computes it afresh."""
    created_at: datetime
    last_fire_at: datetime | None = None
    last_session_id: str | None = None
    """The session the last run started, so a run still working holds the next one back."""


class ScheduleStore(Protocol):
    """Schedules, in the domain store so a change wakes the store watcher (DEC-090)."""

    async def add(self, schedule: Schedule) -> None: ...

    async def get(self, schedule_id: str) -> Schedule | None: ...

    async def list(self) -> tuple[Schedule, ...]: ...

    async def set_paused(
        self, schedule_id: str, paused: bool, *, next_fire_at: datetime | None
    ) -> Schedule | None:
        """Pause or resume, setting the next fire with it; None when there is no such row."""
        ...

    async def delete(self, schedule_id: str) -> bool:
        """True when a row was removed."""
        ...

    async def due(self, now: datetime) -> tuple[Schedule, ...]:
        """Unpaused schedules whose next fire is at or before `now`, oldest first."""
        ...

    async def record_fire(
        self,
        schedule_id: str,
        fired_at: datetime,
        session_id: str | None,
        next_fire_at: datetime | None,
    ) -> None:
        """Advance a schedule past one fire. `next_fire_at` None means it is done (a one-shot),
        and the row is deleted. `session_id` None keeps the previous run's session."""
        ...
