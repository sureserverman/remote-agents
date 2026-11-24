"""Telegram schedule management and scheduled-launch wizard."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from html import escape
from math import ceil

from remote_agents.adapters.telegram.launch_resume import _split_launch
from remote_agents.adapters.telegram.navigation import _button_rows, _page_number, notice_line
from remote_agents.adapters.telegram.presenters import Button, RenderedMessage
from remote_agents.adapters.telegram.screen import ScreenActions
from remote_agents.adapters.telegram.screen import reply_arguments as _reply_arguments
from remote_agents.application.profiles import ProfileAvailability
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
from remote_agents.application.session_views import (
    SCHEDULE_PREVIEW_WIDTH,
    ScheduleLine,
    fire_words,
    project_name,
    repeat_words,
    schedule_lines,
)
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.schedules import Weekday


@dataclass(frozen=True, slots=True)
class _ScheduleDraft:
    """A schedule part-way through being made: what has been chosen so far."""

    project_id: str
    profile_id: str
    at: datetime | None = None
    """The chosen local wall-clock time, naive -- a repeat keeps its time of day alone."""
    repeat: str | None = None
    days: frozenset[int] = frozenset()
    message: str | None = None


_SCHEDULE_REFUSALS = {
    "empty": "The message is empty.",
    "shell": "A message starting with ! would run as a shell command, so it is refused.",
    "menu": "This agent's command menu cannot be read, so a message starting with / is refused.",
    "no_composer": "This agent's screen cannot be read, so nothing can be typed into it.",
    "unknown_project": "That project is no longer available.",
    "unknown_profile": "That agent is not available.",
    "past": "That time has already passed. Choose another.",
}

_TIME_PRESET_LABELS = {
    "in_1h": "in 1h",
    "tonight_0300": "tonight 03:00",
    "tomorrow_0900": "tomorrow 09:00",
}

_REPEAT_LABELS = {"once": "Once", "daily": "Daily", "weekdays": "Weekdays", "days": "Pick days"}

_DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_SCHEDULES_PAGE_SIZE = 8


@dataclass
class ScheduleState:
    draft: _ScheduleDraft | None = None
    page: int = 1
    last_lines: tuple[object, ...] = ()


@dataclass(kw_only=True)
class ScheduleScreens(ScreenActions):
    state: ScheduleState
    mark_screen: Callable[[str], None]
    catalogue: tuple[CatalogProject, ...]
    profiles: tuple[ProfileAvailability, ...]
    _provider_name: Callable[[str], str]

    def accept_text(self, action: str, entity_id: str, value: str) -> RenderedMessage | str:
        """Advance the draft or return the question to ask again."""
        book = self.backend.schedules
        draft = self.state.draft
        if book is None or draft is None:
            return self._message("That schedule is no longer being made. Start it again.")
        if action == "schedule.time":
            at = parse_time_text(value, book.now(), book.zone)
            if at is None:
                return "Use HH:MM, or YYYY-MM-DD HH:MM, in this host's time."
            self.state.draft = replace(draft, at=at)
            return self._schedule_repeat_reply()
        refusal = book.refusal(ProfileId(draft.profile_id), value)
        if refusal is not None:
            return _SCHEDULE_REFUSALS.get(refusal.value, "That message is refused.")
        self.state.draft = replace(draft, repeat=entity_id, message=value)
        return self._schedule_review_reply()

    def text_landing(self, action: str, entity_id: str) -> RenderedMessage | None:
        draft = self.state.draft
        if draft is None:
            return None
        if action == "schedule.message" and draft.message is not None:
            return self._schedule_review_reply()
        if action == "schedule.message" and draft.at is not None:
            if entity_id == "days" and draft.days:
                return self._schedule_days_reply()
            return self._schedule_repeat_reply()
        return self._schedule_time_reply()

    async def _schedules_reply(
        self,
        notice: str | None = None,
        *,
        mark: bool = True,
        lines: tuple[ScheduleLine, ...] | None = None,
    ) -> RenderedMessage:
        """Every schedule as one line of facts over its message's start, with pause or
        resume and delete beside each, numbered in the order listed, a page at a time -- a
        message holds 4096 characters and a keyboard 100 buttons."""
        book = self.backend.schedules
        if book is None:
            return self._message("Scheduling is unavailable.")
        if mark:
            self.mark_screen("schedules")
        if lines is None:
            lines = schedule_lines(await book.list(), self.catalogue, book.zone, book.now())
        if mark:
            # A press draws what it read, so the watcher compares against this. The watcher's
            # own redraw records its lines only once its edit has landed.
            self.state.last_lines = tuple(lines)
        lead = notice_line(notice)
        if not lines:
            return self._message(
                f"{lead}<b>Schedules</b>\nNo schedules. Make one from Launch: choose a project, "
                "then ⏰ Schedule.",
                back=self._callback("sessions.open", "sessions"),
            )
        page_count = max(1, ceil(len(lines) / _SCHEDULES_PAGE_SIZE))
        page = min(max(self.state.page, 1), page_count)
        self.state.page = page
        start = (page - 1) * _SCHEDULES_PAGE_SIZE
        shown = tuple(enumerate(lines, start=1))[start : start + _SCHEDULES_PAGE_SIZE]
        body = "\n\n".join(
            f"<b>{number}.</b> {escape(line.facts)}\n<code>{escape(line.preview)}</code>"
            for number, line in shown
        )
        rows: list[tuple[Button, ...]] = [
            (
                Button(f"▶️ {number}", self._callback("schedules.resume", line.schedule_id))
                if line.paused
                else Button(f"⏸ {number}", self._callback("schedules.pause", line.schedule_id)),
                Button(f"🗑 {number}", self._callback("schedules.delete", line.schedule_id)),
            )
            for number, line in shown
        ]
        navigation = []
        if page > 1:
            navigation.append(Button("Previous", self._callback("schedules.page", str(page - 1))))
        if page < page_count:
            navigation.append(Button("Next", self._callback("schedules.page", str(page + 1))))
        if navigation:
            rows.append(tuple(navigation))
        title = "Schedules" if page_count == 1 else f"Schedules {page}/{page_count}"
        return self._message(
            f"{lead}<b>{title}</b> · {len(lines)}\n\n{body}",
            tuple(rows),
            back=self._callback("sessions.open", "sessions"),
        )

    async def _schedules_action_reply(
        self, action: str, entity_id: str, token: str, message_id: int
    ) -> dict[str, object]:
        book = self.backend.schedules
        if book is None:
            return _reply_arguments(self._message("Scheduling is unavailable."))
        if action == "schedules.open":
            self.state.page = 1
            return _reply_arguments(await self._schedules_reply())
        if action == "schedules.page":
            self.state.page = _page_number(entity_id)
            return _reply_arguments(await self._schedules_reply())
        if action == "schedules.pause":
            paused = await book.pause(entity_id)
            notice = None if paused is not None else "That schedule is no longer there."
            return _reply_arguments(await self._schedules_reply(notice))
        if action == "schedules.resume":
            resumed = await book.resume(entity_id)
            if resumed is None:
                notice = "That schedule is no longer there."
            elif isinstance(resumed, ScheduleRefused):
                notice = "Its one time passed while it was paused, so it was removed."
            else:
                notice = None
            return _reply_arguments(await self._schedules_reply(notice))
        if action == "schedules.delete":
            schedule = next((item for item in await book.list() if item.id == entity_id), None)
            if schedule is None:
                return _reply_arguments(
                    await self._schedules_reply("That schedule is no longer there.")
                )
            (line,) = schedule_lines((schedule,), self.catalogue, book.zone, book.now())
            return _reply_arguments(
                self._message(
                    f"<b>Delete this schedule?</b>\n{escape(line.facts)}\n"
                    f"<code>{escape(line.preview)}</code>",
                    (
                        (
                            Button(
                                "🗑 Delete",
                                self._callback("schedules.deleted", entity_id, mutation=True),
                            ),
                        ),
                    ),
                    back=self._callback("schedules.open", "schedules"),
                )
            )
        if action == "schedules.deleted":
            if not self.callbacks.claim_mutation(
                token,
                owner_id=self.owner_user_id,
                chat_id=self.owner_chat_id,
                message_id=message_id,
            ):
                return _reply_arguments(self._message("That action has already run."))
            deleted = await book.delete(entity_id)
            notice = "Deleted." if deleted else "That schedule is no longer there."
            return _reply_arguments(await self._schedules_reply(notice))
        return _reply_arguments(self._message("That action is no longer available."))

    async def _schedule_step_reply(
        self, action: str, entity_id: str, token: str, message_id: int
    ) -> dict[str, object]:
        """One press of the schedule flow. Each step re-reads the draft, and a draft lost to a
        restart sends the owner back to the start rather than guessing."""
        book = self.backend.schedules
        if book is None:
            return _reply_arguments(self._message("Scheduling is unavailable."))
        if action == "sched.agents":
            return _reply_arguments(self._schedule_agents_reply(entity_id))
        if action == "sched.profile":
            project_id, profile_id = _split_launch(entity_id)
            self.state.draft = _ScheduleDraft(project_id, profile_id)
            return _reply_arguments(self._schedule_time_reply())
        draft = self.state.draft
        if draft is None:
            return _reply_arguments(
                self._message("That schedule is no longer being made. Start it again from Launch.")
            )
        if action == "sched.time":
            self.state.draft = replace(draft, at=preset_time(entity_id, book.now(), book.zone))
            return _reply_arguments(self._schedule_repeat_reply())
        if action == "sched.retime":
            return _reply_arguments(self._schedule_time_reply())
        if action == "sched.repeat" and draft.at is not None:
            return _reply_arguments(self._schedule_repeat_reply())
        if action == "sched.days" and draft.at is not None:
            return _reply_arguments(self._schedule_days_reply())
        if action == "sched.day" and draft.at is not None:
            day = int(entity_id)
            days = draft.days - {day} if day in draft.days else draft.days | {day}
            self.state.draft = replace(draft, days=frozenset(days))
            return _reply_arguments(self._schedule_days_reply())
        if action == "sched.confirm":
            return await self._schedule_confirm_reply(token, message_id)
        return _reply_arguments(self._message("That action is no longer available."))

    def _schedule_agents_reply(self, project_id: str) -> RenderedMessage:
        if not any(project.opaque_id == project_id for project in self.catalogue):
            return self._message("The project is no longer available.")
        buttons = tuple(
            Button(
                self._provider_name(profile.profile_id),
                self._callback("sched.profile", f"{project_id}|{profile.profile_id}"),
            )
            for profile in self.profiles
            if profile.available
        )
        return self._message(
            "<b>Schedule an agent</b>\nChoose the agent to start.",
            _button_rows(buttons),
            back=self._callback("launch.project", project_id),
        )

    def _schedule_heading(self, draft: _ScheduleDraft) -> str:
        project = project_name(draft.project_id, self.catalogue)
        return f"<b>Schedule</b> · {escape(draft.profile_id)} in {escape(project)}"

    def _schedule_time_reply(self, notice: str | None = None) -> RenderedMessage:
        draft = self.state.draft
        assert draft is not None
        presets = tuple(
            Button(_TIME_PRESET_LABELS[key], self._callback("sched.time", key))
            for key in TIME_PRESETS
        )
        lead = f"{escape(notice)}\n" if notice else ""
        return self._message(
            f"{lead}{self._schedule_heading(draft)}\nWhen should it start? Times are this host's "
            "local time.",
            _button_rows(presets, width=3)
            + ((Button("⌨️ Type a time", self._callback("schedule.time", "draft")),),),
            back=self._callback("sched.agents", draft.project_id),
        )

    def _schedule_repeat_reply(self) -> RenderedMessage:
        draft = self.state.draft
        assert draft is not None and draft.at is not None
        repeats = tuple(
            Button(
                _REPEAT_LABELS[key],
                # Every repeat but a choice of days opens the message step at once, carrying
                # the repeat it was chosen with.
                self._callback("sched.days", "draft")
                if key == "days"
                else self._callback("schedule.message", key),
            )
            for key in REPEATS
        )
        return self._message(
            f"{self._schedule_heading(draft)}\nAt {draft.at:%H:%M} ({draft.at:%a %d %b}). "
            "How often?",
            _button_rows(repeats),
            back=self._callback("sched.profile", f"{draft.project_id}|{draft.profile_id}"),
        )

    def _schedule_days_reply(self) -> RenderedMessage:
        draft = self.state.draft
        assert draft is not None and draft.at is not None
        days = tuple(
            Button(
                f"✅ {label}" if index in draft.days else label,
                self._callback("sched.day", str(index)),
            )
            for index, label in enumerate(_DAY_LABELS)
        )
        done = (
            ((Button("Done", self._callback("schedule.message", "days")),),) if draft.days else ()
        )
        return self._message(
            f"{self._schedule_heading(draft)}\nAt {draft.at:%H:%M} on which days?",
            _button_rows(days, width=4) + done,
            back=self._callback("sched.repeat", "draft"),
        )

    def _draft_when(self, draft: _ScheduleDraft):
        assert draft.at is not None and draft.repeat is not None
        return when_from(draft.at, draft.repeat, (Weekday(day) for day in draft.days))

    def _schedule_review_reply(self, notice: str | None = None) -> RenderedMessage:
        draft = self.state.draft
        assert draft is not None and draft.message is not None
        book = self.backend.schedules
        when = self._draft_when(draft)
        fire = next_fire(when, book.now(), book.zone)
        first = " ".join(draft.message.split())
        if len(first) > SCHEDULE_PREVIEW_WIDTH:
            first = first[: SCHEDULE_PREVIEW_WIDTH - 1] + "…"
        lines = [
            self._schedule_heading(draft),
            escape(repeat_words(when)),
            (
                f"next {fire_words(fire, book.zone, book.now())}"
                if fire is not None
                else _SCHEDULE_REFUSALS["past"]
            ),
            f"Message: <code>{escape(first)}</code>",
        ]
        if notice:
            lines.insert(0, escape(notice))
        confirm = (
            ((Button("✅ Confirm", self._callback("sched.confirm", "draft", mutation=True)),),)
            if fire is not None
            # Nothing to confirm: a time already passed is chosen again, not saved.
            else ((Button("⏰ Choose another time", self._callback("sched.retime", "draft")),),)
        )
        return self._message(
            "\n".join(lines),
            confirm
            + (
                (
                    Button(
                        "✏️ Change the message",
                        self._callback("schedule.message", draft.repeat or "once"),
                    ),
                ),
            ),
            back=self._callback("launch.project", draft.project_id),
            back_label="Cancel",
        )

    async def _schedule_confirm_reply(self, token: str, message_id: int) -> dict[str, object]:
        draft = self.state.draft
        book = self.backend.schedules
        assert draft is not None and book is not None
        if draft.message is None or draft.at is None or draft.repeat is None:
            return _reply_arguments(self._schedule_time_reply())
        if not self.callbacks.claim_mutation(
            token, owner_id=self.owner_user_id, chat_id=self.owner_chat_id, message_id=message_id
        ):
            return _reply_arguments(self._message("That action has already run."))
        added = await book.add(
            ProjectId(draft.project_id),
            ProfileId(draft.profile_id),
            draft.message,
            self._draft_when(draft),
        )
        if isinstance(added, ScheduleRefused):
            words = _SCHEDULE_REFUSALS.get(added.reason.value, "That schedule was refused.")
            if added.reason is ScheduleRefusal.PAST:
                return _reply_arguments(self._schedule_time_reply(notice=words))
            return _reply_arguments(self._schedule_review_reply(notice=words))
        self.state.draft = None
        (line,) = schedule_lines((added,), self.catalogue, book.zone, book.now())
        return _reply_arguments(
            self._message(
                f"<b>Scheduled</b>\n{escape(line.facts)}",
                ((Button("⏰ Schedules", self._callback("schedules.open", "schedules")),),),
            )
        )
