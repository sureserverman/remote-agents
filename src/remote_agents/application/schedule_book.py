"""Add, list, pause, resume and delete schedules -- what both surfaces call (DEC-046).

**A prompt is refused when it is saved, by the send's own rule** (`ports.prompt_rules`, DEC-043):
a schedule that could never be typed is refused while the owner is still looking at it, not
reported hours later from an empty pane. Whether `/` is sendable is read from the profile's
composer, which the composition root hands in from the provider descriptors (DEC-070).

**A resume never fires a backlog**: it computes the next fire from now, so a schedule paused
over three of its times fires at the fourth, and a one-shot whose time passed while paused is
deleted on resume rather than fired late. A resume of a schedule that is not paused changes
nothing -- recomputing an active schedule's time would skip a fire that is due.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from enum import StrEnum

from remote_agents.application.schedule_times import next_fire
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.prompt_rules import pre_paste_refusal
from remote_agents.ports.provider_descriptor import ComposerScreen
from remote_agents.ports.schedules import Schedule, ScheduleStore, When


class ScheduleRefusal(StrEnum):
    """Why a schedule was not saved -- a key each surface words (DEC-043)."""

    EMPTY = "empty"
    SHELL = "shell"
    MENU = "menu"
    NO_COMPOSER = "no_composer"
    UNKNOWN_PROJECT = "unknown_project"
    UNKNOWN_PROFILE = "unknown_profile"
    PAST = "past"
    """A one-shot whose time has passed: refused on add, and deleted on a resume."""


@dataclass(frozen=True, slots=True)
class ScheduleRefused:
    reason: ScheduleRefusal


class ScheduleBook:
    """The schedule use cases, over one store and the host's zone."""

    def __init__(
        self,
        store: ScheduleStore,
        *,
        composers: Mapping[str, ComposerScreen | None],
        projects: Callable[[], Iterable[ProjectId]],
        zone: Callable[[], tzinfo],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: secrets.token_hex(4),
    ) -> None:
        self._store = store
        #: Every registered profile, with its composer or None when it declares none.
        self._composers = dict(composers)
        self._projects = projects
        #: Read per call, so a host whose zone changed is never answered from a stale one.
        self._zone = zone
        self._now = now
        self._new_id = new_id

    def now(self) -> datetime:
        """The book's clock, which a surface offering "in 1h" or "tomorrow" measures from."""
        return self._now()

    def refusal(self, profile_id: ProfileId, prompt: str) -> ScheduleRefusal | None:
        """Whether `add` would refuse this message for this agent -- asked by a surface at the
        message step, so the owner is told while still typing it (rule 3)."""
        if str(profile_id) not in self._composers:
            return ScheduleRefusal.UNKNOWN_PROFILE
        refusal = pre_paste_refusal(prompt, self._composers[str(profile_id)])
        return None if refusal is None else ScheduleRefusal(refusal.value)

    async def add(
        self, project_id: ProjectId, profile_id: ProfileId, prompt: str, when: When
    ) -> Schedule | ScheduleRefused:
        if project_id not in set(self._projects()):
            return ScheduleRefused(ScheduleRefusal.UNKNOWN_PROJECT)
        refusal = self.refusal(profile_id, prompt)
        if refusal is not None:
            return ScheduleRefused(refusal)
        now = self._now()
        fire = next_fire(when, now, self._zone())
        if fire is None:
            return ScheduleRefused(ScheduleRefusal.PAST)
        schedule = Schedule(
            id=self._new_id(),
            project_id=project_id,
            profile_id=profile_id,
            prompt=prompt,
            when=when,
            paused=False,
            next_fire_at=fire,
            created_at=now,
        )
        await self._store.add(schedule)
        return schedule

    async def list(self) -> tuple[Schedule, ...]:
        """Soonest first; paused schedules, which have no next time, last."""
        schedules = await self._store.list()
        return tuple(
            sorted(
                schedules,
                key=lambda schedule: (
                    schedule.next_fire_at is None,
                    schedule.next_fire_at or schedule.created_at,
                    schedule.id,
                ),
            )
        )

    async def pause(self, schedule_id: str) -> Schedule | None:
        return await self._store.set_paused(schedule_id, True, next_fire_at=None)

    @property
    def zone(self) -> tzinfo:
        """The zone schedules are read in, now -- for a surface showing a local time."""
        return self._zone()

    async def resume(self, schedule_id: str) -> Schedule | ScheduleRefused | None:
        """Resume from now; None for an unknown schedule. A one-shot whose time passed while
        it was paused can no longer fire: it is deleted, and the answer says why."""
        schedule = await self._store.get(schedule_id)
        if schedule is None:
            return None
        if not schedule.paused:
            return schedule
        fire = next_fire(schedule.when, self._now(), self._zone())
        if fire is None:
            await self._store.delete(schedule_id)
            return ScheduleRefused(ScheduleRefusal.PAST)
        return await self._store.set_paused(schedule_id, False, next_fire_at=fire)

    async def delete(self, schedule_id: str) -> bool:
        return await self._store.delete(schedule_id)
