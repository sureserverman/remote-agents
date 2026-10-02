"""Scheduled sessions on the local surface (DEC-114): make one, and list a project's.

**One position for the whole making of a schedule**, stepped through in place -- agent, time,
repeat, days, message, review -- rather than a screen per step. Each step is a question the
bot asks too, in the same order and with the same choices; what both surfaces save is decided by
`Backend.schedules`, and a message is refused at its step by the send's own rule
(`ScheduleBook.refusal`). The listing reads its lines from `session_views.schedule_lines`, the
function the bot reads (DEC-091), so the two surfaces cannot disagree about a schedule's facts.

Firing is the bot's service alone; nothing here fires anything.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from textual.widgets import Input

from remote_agents.adapters.tui.model import _BACK
from remote_agents.adapters.tui.screens.base import NEVER_EMPTY, ChoiceScreen
from remote_agents.adapters.tui.screens.confirm import ConfirmScreen
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.schedule_book import ScheduleRefusal, ScheduleRefused
from remote_agents.application.schedule_times import (
    REPEATS,
    TIME_PRESETS,
    next_fire,
    parse_time_text,
    preset_time,
    when_from,
)
from remote_agents.application.session_views import fire_words, repeat_words, schedule_lines
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.schedules import Weekday

#: A refused schedule in this surface's words (DEC-043: the key is shared, the sentence is not).
_REFUSALS = {
    "empty": "The message is empty.",
    "shell": "A message starting with ! would run as a shell command, so it is refused.",
    "menu": "This agent's command menu cannot be read, so a message starting with / is refused.",
    "no_composer": "This agent's screen cannot be read, so nothing can be typed into it.",
    "unknown_project": "That project is no longer available.",
    "unknown_profile": "That agent is not available.",
    "past": "That time has already passed. Choose another.",
}
_PRESET_LABELS = {
    "in_1h": "In 1 hour",
    "tonight_0300": "Tonight at 03:00",
    "tomorrow_0900": "Tomorrow at 09:00",
}
_REPEAT_LABELS = {"once": "Once", "daily": "Daily", "weekdays": "Weekdays", "days": "Pick days"}
_DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_TYPE = "type"
_DONE = "done"
_CONFIRM = "confirm"


@dataclass(frozen=True, slots=True)
class _Draft:
    profile_id: str | None = None
    at: datetime | None = None
    repeat: str | None = None
    days: frozenset[int] = frozenset()
    message: str | None = None


class ScheduleScreen(ChoiceScreen):
    """Make one schedule for one project, a step at a time, saved only at the last."""

    empty_state = NEVER_EMPTY
    position = "SCHEDULE"
    status = "Choose the agent to start."
    entry_is_a_commitment = True

    def __init__(self, project: CatalogProject, profile_id: str | None = None) -> None:
        super().__init__()
        self.project = project
        # Opened with an agent already chosen, the flow starts at the time.
        self._draft = _Draft(profile_id=profile_id)
        self._step = "agent" if profile_id is None else "time"

    @property
    def work_in_flight(self) -> bool:
        """Anything chosen so far is work escape cannot give back: the flow is one position,
        so leaving it loses every step at once."""
        return self._draft != _Draft() or super().work_in_flight

    @property
    def work_at_risk(self) -> str:
        """The schedule as far as it has been made, so a warning can say what it discards."""
        typed = super().work_at_risk
        if typed:
            return typed
        if self._draft == _Draft():
            return ""
        return f"a schedule for {self._draft.profile_id} in {self.project.name}"

    @property
    def crumb(self) -> str:
        return "Schedule"

    @property
    def _book(self):
        return self.services.backend.schedules

    async def populate(self) -> None:
        self._render_step()

    def _render_step(self, notice: str | None = None) -> None:
        if not self.showing:
            return
        step = self._step
        if step == "agent":
            self.hide_entry()
            self.set_status(notice or "Choose the agent to start.")
            entries = tuple(
                (profile.profile_id, profile.profile_id)
                for profile in self.services.profiles
                if profile.available
            )
            self.show_choices(entries, highlight=len(entries), trailing=((_BACK, "Back"),))
        elif step == "time":
            self.hide_entry()
            self.set_status(notice or "When should it start? Times are this host's local time.")
            entries = tuple((key, _PRESET_LABELS[key]) for key in TIME_PRESETS)
            self.show_choices(
                (*entries, (_TYPE, "Type a time…")),
                highlight=len(entries) + 1,
                trailing=((_BACK, "Back"),),
            )
        elif step == "typed_time":
            self.text_entry("HH:MM, or YYYY-MM-DD HH:MM")
            words = notice or "Type a time -- HH:MM, or YYYY-MM-DD HH:MM -- then press enter."
            self.set_status(words, severity="warning" if notice else "information")
        elif step == "repeat":
            self.hide_entry()
            at = self._draft.at
            assert at is not None
            self.set_status(notice or f"At {at:%H:%M} ({at:%a %d %b}). How often?")
            entries = tuple((key, _REPEAT_LABELS[key]) for key in REPEATS)
            self.show_choices(entries, highlight=len(entries), trailing=((_BACK, "Back"),))
        elif step == "days":
            self.hide_entry()
            at = self._draft.at
            assert at is not None
            self.set_status(notice or f"At {at:%H:%M} on which days? Choose each, then Done.")
            entries = tuple(
                (str(index), f"[x] {label}" if index in self._draft.days else f"[ ] {label}")
                for index, label in enumerate(_DAY_LABELS)
            )
            done = ((_DONE, "Done"),) if self._draft.days else ()
            self.show_choices((*entries, *done), highlight=None, trailing=((_BACK, "Back"),))
        elif step == "message":
            self.text_entry("The message to type when the session starts")
            words = notice or "Type the message to send when the session starts, then press enter."
            self.set_status(words, severity="warning" if notice else "information")
        elif step == "review":
            self.hide_entry()
            self._render_review(notice)

    def _render_review(self, notice: str | None) -> None:
        draft = self._draft
        assert draft.at is not None and draft.repeat is not None and draft.message is not None
        when = when_from(draft.at, draft.repeat, (Weekday(day) for day in draft.days))
        book = self._book
        fire = next_fire(when, book.now(), book.zone)
        nxt = (
            f"next {fire_words(fire, book.zone, book.now())}"
            if fire is not None
            else _REFUSALS["past"]
        )
        first = " ".join(draft.message.split())
        words = notice or f"{draft.profile_id} · {self.project.name} · {repeat_words(when)} · {nxt}"
        self.set_status(words, severity="warning" if notice else "information")
        self.show_choices(
            (("message", f"Message: {first}"), (_CONFIRM, "Save this schedule")),
            highlight=0,
            trailing=((_BACK, "Back"),),
        )

    def _back_one(self) -> str | None:
        """The step before this one, or None when this is the first."""
        return {
            "time": "agent",
            "typed_time": "time",
            "repeat": "time",
            "days": "repeat",
            "message": "days" if self._draft.repeat == "days" else "repeat",
            "review": "message",
        }.get(self._step)

    async def choose(self, key: str) -> None:
        if key == _BACK:
            previous = self._back_one()
            if previous is None:
                await self.tui.go_back()
                return
            self._step = previous
            self._render_step()
            return
        book = self._book
        if book is None:
            self.announce("Scheduling is unavailable on this host.")
            return
        step = self._step
        if step == "agent":
            self._draft = replace(self._draft, profile_id=key)
            self._step = "time"
        elif step == "time":
            if key == _TYPE:
                self._step = "typed_time"
            else:
                self._draft = replace(self._draft, at=preset_time(key, book.now(), book.zone))
                self._step = "repeat"
        elif step == "repeat":
            self._draft = replace(self._draft, repeat=key)
            self._step = "days" if key == "days" else "message"
        elif step == "days":
            if key == _DONE:
                self._step = "message"
            else:
                day = int(key)
                days = self._draft.days
                self._draft = replace(
                    self._draft, days=days - {day} if day in days else days | {day}
                )
        elif step == "review":
            if key == "message":
                self._step = "message"
            elif key == _CONFIRM:
                await self._save()
                return
        self._render_step()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.submit(event.value)

    def submit(self, value: str) -> None:
        """A typed time or the message, each checked here while the owner is still at it."""
        book = self._book
        if book is None:
            return
        if self._step == "typed_time":
            at = parse_time_text(value, book.now(), book.zone)
            if at is None:
                self._render_step("Use HH:MM, or YYYY-MM-DD HH:MM, in this host's time.")
                return
            self._draft = replace(self._draft, at=at)
            self._step = "repeat"
            self._render_step()
            return
        if self._step == "message":
            assert self._draft.profile_id is not None
            refusal = book.refusal(ProfileId(self._draft.profile_id), value)
            if refusal is not None:
                self._render_step(_REFUSALS.get(refusal.value, "That message is refused."))
                return
            self._draft = replace(self._draft, message=value)
            self._step = "review"
            self._render_step()

    async def _save(self) -> None:
        draft = self._draft
        book = self._book
        assert draft.profile_id and draft.at and draft.repeat and draft.message is not None
        added = await book.add(
            ProjectId(self.project.opaque_id),
            ProfileId(draft.profile_id),
            draft.message,
            when_from(draft.at, draft.repeat, (Weekday(day) for day in draft.days)),
        )
        if isinstance(added, ScheduleRefused):
            words = _REFUSALS.get(added.reason.value, "That schedule was refused.")
            self._step = "time" if added.reason is ScheduleRefusal.PAST else "review"
            self._render_step(words)
            return
        (line,) = schedule_lines((added,), (self.project,), book.zone, book.now())
        self.announce(f"Scheduled: {line.facts}", severity="information")
        await self.tui.go_back()


class ScheduleDeleteConfirmModal(ConfirmScreen):
    """Delete a schedule: the question, with Cancel under the cursor."""

    position = "SCHEDULE_DELETE_MODAL"
    question = "Delete this schedule? It will not start again."
    confirm_key = "delete"
    confirm_label = "Delete the schedule"


class SchedulesScreen(ChoiceScreen):
    """One project's schedules, each with pause or resume and delete."""

    empty_state = "This project has no schedules. Choose Schedule a session to make one."
    position = "SCHEDULES"
    status = "Choose a schedule to pause, resume or delete it."
    can_refresh = True

    def __init__(self, project: CatalogProject) -> None:
        super().__init__()
        self.project = project
        #: The schedule whose actions are on screen, or None while the list is.
        self._acting_on: str | None = None

    @property
    def crumb(self) -> str:
        return "Schedules"

    async def populate(self) -> None:
        await self._render_list()

    async def refresh_contents(self) -> None:
        await self._render_list()

    async def _lines(self):
        book = self.services.backend.schedules
        if book is None:
            return ()
        mine = tuple(
            schedule
            for schedule in await book.list()
            if str(schedule.project_id) == self.project.opaque_id
        )
        return schedule_lines(mine, (self.project,), book.zone, book.now())

    async def _render_list(self, notice: str | None = None) -> None:
        if not self.showing:
            return
        self.hide_entry()
        self._acting_on = None
        lines = await self._lines()
        self.set_status(notice or self.status)
        self.show_choices(
            tuple((line.schedule_id, f"{line.facts}  —  {line.preview}") for line in lines),
            highlight=len(lines),
            trailing=((_BACK, "Back"),),
        )

    async def _render_actions(self, schedule_id: str) -> None:
        line = next((item for item in await self._lines() if item.schedule_id == schedule_id), None)
        if line is None:
            await self._render_list("That schedule is no longer there.")
            return
        self._acting_on = schedule_id
        self.set_status(f"{line.facts}  —  {line.preview}")
        toggle = ("resume", "Resume") if line.paused else ("pause", "Pause")
        self.show_choices(
            (toggle, ("delete", "Delete…")),
            highlight=2,
            trailing=((_BACK, "Back"),),
        )

    async def choose(self, key: str) -> None:
        book = self.services.backend.schedules
        if key == _BACK:
            if self._acting_on is not None:
                await self._render_list()
                return
            await self.tui.go_back()
            return
        if book is None:
            return
        if self._acting_on is None:
            await self._render_actions(key)
            return
        schedule_id = self._acting_on
        if key == "pause":
            await book.pause(schedule_id)
            await self._render_list()
        elif key == "resume":
            resumed = await book.resume(schedule_id)
            notice = (
                "Its one time passed while it was paused, so it was removed."
                if isinstance(resumed, ScheduleRefused)
                else None
            )
            await self._render_list(notice)
        elif key == "delete":
            async with self.holding_the_guard():
                confirmed = await self.tui.ask_to_confirm(ScheduleDeleteConfirmModal())
            if confirmed:
                await book.delete(schedule_id)
                await self._render_list("Deleted.")
            else:
                await self._render_actions(schedule_id)
