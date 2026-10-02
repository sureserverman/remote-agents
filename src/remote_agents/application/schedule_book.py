"""Add, list, pause, resume and delete schedules -- what both surfaces call (DEC-046).

**A prompt is refused when it is saved, by the send's own rule** (`ports.prompt_rules`, DEC-043):
a schedule that could never be typed is refused while the owner is still looking at it, not
reported hours later from an empty pane. Whether `/` is sendable is read from the profile's
composer, which the composition root hands in from the provider descriptors (DEC-070).

**A resume never fires a backlog**: it computes the next fire from now, so a schedule paused
over three of its times fires at the fourth, and a one-shot whose time passed while paused is
deleted on resume rather than fired late.
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
        zone: tzinfo,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: secrets.token_hex(4),
    ) -> None:
        self._store = store
        #: Every registered profile, with its composer or None when it declares none.
        self._composers = dict(composers)
        self._projects = projects
        self.zone = zone
        self._now = now
        self._new_id = new_id

    async def add(
        self, project_id: ProjectId, profile_id: ProfileId, prompt: str, when: When
    ) -> Schedule | ScheduleRefused:
        if project_id not in set(self._projects()):
            return ScheduleRefused(ScheduleRefusal.UNKNOWN_PROJECT)
        if str(profile_id) not in self._composers:
            return ScheduleRefused(ScheduleRefusal.UNKNOWN_PROFILE)
        refusal = pre_paste_refusal(prompt, self._composers[str(profile_id)])
        if refusal is not None:
            return ScheduleRefused(ScheduleRefusal(refusal.value))
        now = self._now()
        fire = next_fire(when, now, self.zone)
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

    async def resume(self, schedule_id: str) -> Schedule | None:
        """Resume from now. None for an unknown schedule, or a one-shot whose time passed while
        it was paused -- that one is deleted, since it can no longer fire."""
        schedule = await self._store.get(schedule_id)
        if schedule is None:
            return None
        fire = next_fire(schedule.when, self._now(), self.zone)
        if fire is None:
            await self._store.delete(schedule_id)
            return None
        return await self._store.set_paused(schedule_id, False, next_fire_at=fire)

    async def delete(self, schedule_id: str) -> bool:
        return await self._store.delete(schedule_id)
