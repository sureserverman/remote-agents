"""Local terminal surface mirroring the bot wizard, then attaching to what it started."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import WorkerCancelled, WorkerFailed

from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.commands import (
    CleanupCommand,
    ForceStopCommand,
    GracefulStopCommand,
    LaunchCommand,
    RemoteControlCommand,
    ResumeCommand,
)
from remote_agents.application.conversations import ConversationCatalogueQuery
from remote_agents.application.project_admin import CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject, search_catalogue
from remote_agents.application.session_actions import (
    ACTION_LABELS as _ACTION_LABELS,
)
from remote_agents.application.session_actions import (
    CLEANUP,
    FORCE,
    GRACEFUL,
    available_actions,
    explain_state,
    remote_control_available,
)
from remote_agents.domain.conversations import ConversationReference, ConversationSummary
from remote_agents.domain.models import ProfileId, ProjectId, SessionRecord, SessionState
from remote_agents.domain.projects import ProjectIdentity
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.ports.terminal_text import sanitize_terminal_text

_LOG = logging.getLogger(__name__)
_T = TypeVar("_T")


class Step(StrEnum):
    """The wizard positions, each deciding which handler may act on an event."""

    PROJECTS = "projects"
    PROFILES = "profiles"
    LABEL = "label"
    REVIEW = "review"
    AREAS = "areas"
    NAME = "name"
    PROJECT_REVIEW = "project-review"
    SESSIONS = "sessions"
    SESSION_DETAIL = "session-detail"
    FORCE_CONFIRM = "force-confirm"
    REMOTE_CONTROL_CONFIRM = "remote-control-confirm"
    INSPECT = "inspect"
    RESUME_PROJECTS = "resume-projects"
    RESUME_PROFILES = "resume-profiles"
    RESUME_CONVERSATIONS = "resume-conversations"
    RESUME_CONFIRM = "resume-confirm"


_TEXT_STEPS = frozenset({Step.LABEL, Step.NAME})
_RESUME_PAGE_SIZE = 10
_INSPECT_MAX_LINES = 2000
_INSPECT_MAX_BYTES = 512 * 1024
_NEXT = "\x00next"
_PREVIOUS = "\x00previous"
_BACK = "\x00back"
_CANCEL = "\x00cancel"


@dataclass(frozen=True, slots=True)
class LaunchSelection:
    """What the wizard has gathered so far, and nothing the surface has not been given."""

    project: CatalogProject | None = None
    profile: ProfileChoice | None = None
    label: str | None = None

    def review(self) -> str:
        project = self.project.name if self.project else "?"
        area = self.project.area if self.project else "?"
        profile = self.profile.profile_id if self.profile else "?"
        label = self.label or "none"
        return f"Project: {area}/{project}\nAgent: {profile}\nLabel: {label}"


@dataclass(frozen=True, slots=True)
class AttachRequest:
    """The one command the app hands back to its caller after a ready launch."""

    session_id: str
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.session_id or not self.argv:
            raise ValueError("an attach request needs a session and a command")

    @property
    def command(self) -> str:
        return " ".join(self.argv)


def label_or_error(value: str, limit: int) -> str | None:
    """Normalize an optional session label under the configured bound."""
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) > limit or any(not character.isprintable() for character in normalized):
        raise ValueError(f"use a visible label of up to {limit} characters")
    return normalized


def selectable_area(value: str) -> bool:
    """Offer an existing directory only when the project identity rule also accepts it."""
    try:
        ProjectIdentity(area=value, name=value)
    except ValueError:
        return False
    return True


class RemoteAgentsTui(App[AttachRequest | None]):
    """Choose a project and an agent, launch it, and hand back an attach command."""

    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #status { height: auto; padding: 0 1; }
    OptionList { height: 1fr; }
    #output { height: 1fr; padding: 0 1; }
    """
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("ctrl+n", "add_project", "Add project"),
        Binding("ctrl+s", "sessions", "Sessions"),
        Binding("ctrl+o", "resume", "Resume"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, context: TuiContext) -> None:
        super().__init__()
        self._services = context
        self._catalogue = context.catalogue
        self._selection = LaunchSelection()
        self._step = Step.PROJECTS
        self._area: str | None = None
        self._name: str | None = None
        self._busy = False
        self._detail_id: str | None = None
        self._resume_project: CatalogProject | None = None
        self._resume_profile: str | None = None
        self._resume_page = 1
        self._resume_page_count = 1
        self._resume_choice: object | None = None
        self._status = "Choose a project."
        # Bumped by every `_fill`; a deferred cursor placement carries the value it was
        # scheduled with and stands down if a later fill has superseded it.
        self._resting_generation = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            # `markup=False` on both Statics for the reason given at `#choices` below, and it
            # is the same defect: these two sinks receive the same untrusted strings by
            # a different route. `#status` is handed the conversation description
            # (`_resolve_resume_conversation`) and `record.display.rendered`, which
            # interpolates the owner's custom label; `#output` is handed the session's raw
            # captured pane output, which `sanitize_terminal_text` filters for control
            # sequences and NUL but not for brackets. Both raised `MarkupError` on an
            # unbalanced bracket — an agent's own output could take down the screen showing it.
            yield Static(self._status, id="status", markup=False)
            yield Input(placeholder="Filter projects", id="filter")
            # `markup=False` because row text is displayed, never interpreted. Three sources
            # reach the rows that console markup would otherwise consume or act on: the
            # project list's `[Registered]` tag, which vanished outright; the owner's own
            # session label; and the conversation description, which is echoed from the
            # agent's own output — where `[link=…]` is a live hyperlink directive and an
            # unbalanced bracket raises `MarkupError`, taking the screen down rather than
            # mangling it. Set once here rather than escaped at each call site so a fourth
            # source cannot arrive unescaped, which is how all three of these went unnoticed.
            #
            # It has to be *here* and not on the rows: `OptionList` renders each `Option`
            # with `visualize(self, option.prompt, markup=self._markup)`, so the flag lives
            # on the widget and `Option` has no `markup` argument at all. Passing it to
            # `Option` would be a `TypeError`; forgetting it here is silent.
            yield OptionList(id="choices", markup=False)
            with VerticalScroll(id="output-pane"):
                yield Static("", id="output", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#output-pane").display = False
        self._show_projects()

    # Rendering -----------------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self._status = text
        self.query_one("#status", Static).update(text)

    async def _in_thread(self, work: Callable[[], _T], *, group: str) -> _T:
        """Run one blocking call on a worker thread and return what it returned.

        The worker is owned by this app, so it is cancelled when the app goes away rather
        than being left to write into a torn-down screen. The `group` is a **label only**:
        `run_worker`'s `exclusive` defaults to `False` and is deliberately not passed here
        (DEC-008), so nothing supersedes anything — it exists to make a flow identifiable in
        worker introspection. The cancellation is of the *worker*, not of the OS thread: a
        blocking call already in progress runs to completion and only its result is dropped.

        `exit_on_error=False` because these calls read a development root and a registry on
        a host that may be misconfigured — that is an error each caller already reports and
        recovers from, not a reason to take the app down. `WorkerFailed` is unwrapped for
        the same reason: callers put the failure on screen, and "Worker raised exception:
        OSError(...)" is not what the owner needs to read.

        `WorkerCancelled` becomes `CancelledError` instead of being unwrapped. It means the
        app is shutting down, not that the read failed, and the callers' `except Exception`
        would otherwise catch it and try to render an error into a screen being torn down.
        `CancelledError` is a `BaseException`, so it passes through those handlers untouched
        — which is what the plain `await` these calls replaced already did.
        """
        worker = self.run_worker(work, thread=True, group=group, exit_on_error=False)
        try:
            return await worker.wait()
        except WorkerFailed as failure:
            raise failure.error from None
        except WorkerCancelled as cancelled:
            raise asyncio.CancelledError(str(cancelled)) from None

    @work
    async def _ask(self, screen: Screen[_T]) -> _T:
        """Push a screen and wait for the answer it is dismissed with.

        This exists to be a **worker context**, which is the one thing the surface did not
        have. `push_screen_wait` calls `get_current_worker()` and raises `NoActiveWorker`
        unless it is already running in a worker (`textual/app.py:2958-2964`), and every
        handler here runs on the message pump, which is not one. `_in_thread` does not help:
        its worker runs the blocking call, while its *caller* still awaits from the pump.

        `exclusive` is not passed, and must not be (DEC-008): a confirmation that cancelled
        the one in flight would dismiss an unanswered modal and start a second, which is the
        cancel-on-re-entry the master gate sweeps for.

        Callers take the result with `await self._ask(screen).wait()` — the decorator returns
        a `Worker`, and it is the body that runs inside the context, so awaiting it from a
        handler is correct.
        """
        return await self.push_screen_wait(screen)

    def _fill(
        self, entries: tuple[tuple[str, str], ...], *, focus: bool = True, highlight: int = 0
    ) -> None:
        """Render the choices, and take the keyboard only when the list is the next decision.

        Refilling while the owner is typing a filter must leave the keyboard where it is, or
        every character after the first lands on the list instead of the query.
        """
        # Restoring here rather than in each exit route: the inspect screen swaps the list
        # for a scrollable output pane, and every other screen renders through _fill, so
        # this is the one place that cannot be forgotten by a new navigation path.
        self._hide_output()
        self._resting_generation += 1
        choices = self.query_one("#choices", OptionList)
        choices.clear_options()
        # The key is the `Option`'s own `id`, which is what the selection message carries
        # back as `option_id`. It replaces the attribute this used to bolt onto each mounted
        # row: `OptionList` mounts no widget per row, so there is nothing to attach to, and
        # row identity is first-class rather than monkey-patched.
        choices.add_options(Option(text, id=key) for key, text in entries)
        if entries and focus:
            resting = min(highlight, len(entries) - 1)
            # Set twice, deliberately. Here, so the cursor is correct the instant `_fill`
            # returns — a keypress arriving before the next refresh must still land on the
            # resting row, which is what keeps a stray enter harmless. Unlike the mounted
            # rows this replaced, `add_options` populates the option list synchronously, so
            # this assignment alone already both decides the enter and draws the cursor:
            # `render_line` compares `self.highlighted` against the row it is painting.
            choices.highlighted = resting
            choices.focus()
            # And again after the refresh, for what this assignment cannot do yet: the widget
            # may still have no `scrollable_content_region` (it is un-hidden a few lines above,
            # and a fresh screen has not laid out), so `watch_highlighted`'s
            # `scroll_to_highlight` finds no line for the index and returns without scrolling.
            # A resting row below the fold would therefore be highlighted but off screen.
            self.call_after_refresh(self._rest_cursor, choices, resting, self._resting_generation)

    def _rest_cursor(self, choices: OptionList, index: int, generation: int) -> None:
        """Re-assert the cursor on `index` once the list has a laid-out region to scroll in.

        `generation` is what makes this safe to defer. The index was computed against the
        entries of one particular fill, and `OptionList.validate_highlighted` *clamps* rather
        than rejects, so a callback that outlived its screen would silently rest the cursor on
        some unrelated row of whatever list is showing now — on a destructive confirm, that
        is the DEC-007 mitigation this method exists to restore, quietly undone.

        **Corrected after review:** an earlier version of this paragraph claimed no path
        reached that "because every `_fill` caller awaits fully between fills", and that
        Stage 2's move to workers was what would make it reachable. That was wrong when it
        was written, not merely overtaken. `_show_areas` and the catalogue refresh already
        awaited off the event loop through the raw thread offload these workers replaced,
        and an `await` yields to the pump identically either way — so a second fill could
        already interleave. Stage 2 changed the mechanism, not the reachability.

        Note what this guard does and does not cover: it protects the *deferred cursor
        placement* only. The `_fill` call in `_show_areas` and `action_refresh` runs
        synchronously when the worker resolves and can still repaint a screen the owner has
        since navigated away from — BL-016, which the screen rewrite closes structurally.

        The highlight is cleared first because `_fill` has usually already assigned this exact
        value, and a reactive assigned its current value notifies nothing — so without the
        clear `watch_highlighted` would not run a second time and the deferred pass would
        achieve nothing at all. What that second run is *for* has changed with the widget:
        the drawn cursor no longer depends on it (`render_line` reads `highlighted` directly,
        so the value `_fill` set is already on screen), but `scroll_to_highlight` does, and
        that is the part `_fill` is too early to complete.

        `watch_highlighted` returns immediately on `None`, so the clear posts nothing —
        subscribers see one `OptionHighlighted` per fill, not a None-then-real pair.
        """
        if generation != self._resting_generation or not choices.options:
            return
        choices.highlighted = None
        choices.highlighted = index

    def _text_entry(self, placeholder: str) -> None:
        """Hand the keyboard to the input, which only the text steps ever use."""
        self._fill(())
        entry = self.query_one("#filter", Input)
        entry.display = True
        entry.value = ""
        entry.placeholder = placeholder
        entry.focus()

    def _hide_entry(self) -> None:
        entry = self.query_one("#filter", Input)
        entry.value = ""
        entry.display = False

    def _show_projects(self, query: str = "", *, keep_focus: bool = False) -> None:
        self._step = Step.PROJECTS
        self._area = None
        self._name = None
        entry = self.query_one("#filter", Input)
        entry.display = True
        entry.placeholder = "Filter projects"
        projects = search_catalogue(self._catalogue, query) if query else self._catalogue
        self._set_status(
            f"Choose a project. {len(projects)} available. "
            "Type to filter, then press enter for the list."
        )
        self._fill(
            tuple(
                (project.opaque_id, f"{project.area}/{project.name}  [{project.group}]")
                for project in projects
            ),
            focus=False,
        )
        if not keep_focus:
            entry.value = ""
            entry.focus()

    def _show_profiles(self) -> None:
        self._step = Step.PROFILES
        self._hide_entry()
        self._set_status("Choose an agent.")
        self._fill(
            tuple(
                (
                    profile.profile_id,
                    profile.profile_id
                    if profile.available
                    else f"{profile.profile_id}  (unavailable: {profile.reason})",
                )
                for profile in self._services.profiles
            )
        )

    def _show_review(self) -> None:
        self._step = Step.REVIEW
        self._hide_entry()
        self._set_status(f"Review\n{self._selection.review()}")
        self._fill((("launch", "Launch"), (_BACK, "Back"), (_CANCEL, "Cancel")), highlight=1)

    async def _show_areas(self) -> None:
        self._step = Step.AREAS
        self._hide_entry()
        try:
            offered = await self._in_thread(self._services.creator.available_areas, group="areas")
        except Exception:
            _LOG.exception("listing areas failed")
            self._set_status("The development root could not be read. Check this host.")
            self._fill(((_CANCEL, "Back"),))
            return
        areas = tuple(area for area in offered if selectable_area(area))
        if not areas:
            self._set_status("No area is available for a new project.")
            self._fill(((_CANCEL, "Back"),))
            return
        self._set_status("Choose the area for the new project.")
        self._fill(tuple((area, area) for area in areas) + ((_CANCEL, "Back"),))

    def _show_project_review(self) -> None:
        """Name the project before creating it, exactly as the bot's Review does."""
        self._step = Step.PROJECT_REVIEW
        self._hide_entry()
        self._set_status(f"Review new project\nArea: {self._area}\nName: {self._name}")
        self._fill((("create", "Create"), (_BACK, "Back"), (_CANCEL, "Cancel")), highlight=1)

    # Interaction ---------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._step is Step.PROJECTS:
            self._show_projects(event.value, keep_focus=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._step is Step.NAME:
            self._submit_name(event.value)
        elif self._step is Step.LABEL:
            self._submit_label(event.value)
        elif self._step is Step.PROJECTS:
            self._enter_project_list()

    def _enter_project_list(self) -> None:
        """Move from the filter into the filtered list, so arrows and enter can pick."""
        choices = self.query_one("#choices", OptionList)
        if choices.options:
            choices.highlighted = 0
            choices.focus()

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Every row `_fill` builds carries its key as the option's id, so a `None` here means
        # a row this app did not construct — refuse it rather than dispatch on it.
        key = event.option_id
        if key is None or self._busy:
            return
        if self._step is Step.PROJECTS:
            self._choose_project(key)
        elif self._step is Step.PROFILES:
            self._choose_profile(key)
        elif self._step is Step.REVIEW:
            await self._resolve_review(key)
        elif self._step is Step.AREAS:
            await self._choose_area(key)
        elif self._step is Step.PROJECT_REVIEW:
            await self._resolve_project_review(key)
        elif self._step is Step.SESSIONS:
            await self._show_detail(key)
        elif self._step is Step.SESSION_DETAIL:
            await self._resolve_detail(key)
        elif self._step is Step.FORCE_CONFIRM:
            await self._resolve_force_confirm(key)
        elif self._step is Step.REMOTE_CONTROL_CONFIRM:
            await self._resolve_remote_control(key)
        elif self._step is Step.RESUME_PROJECTS:
            await self._resolve_resume_project(key)
        elif self._step is Step.RESUME_PROFILES:
            await self._resolve_resume_profile(key)
        elif self._step is Step.RESUME_CONVERSATIONS:
            await self._resolve_resume_conversation(key)
        elif self._step is Step.RESUME_CONFIRM:
            await self._resolve_resume_confirm(key)

    def _choose_project(self, opaque_id: str) -> None:
        project = next((item for item in self._catalogue if item.opaque_id == opaque_id), None)
        if project is None:
            self._set_status("That project is no longer available. Refresh and try again.")
            return
        self._selection = replace(LaunchSelection(), project=project)
        self._show_profiles()

    def _choose_profile(self, profile_id: str) -> None:
        profile = next(
            (item for item in self._services.profiles if item.profile_id == profile_id), None
        )
        if profile is None or not profile.available:
            reason = profile.reason if profile is not None else "unknown profile"
            self._set_status(f"That agent cannot be launched here: {reason}")
            return
        self._selection = replace(self._selection, profile=profile)
        self._step = Step.LABEL
        self._set_status("Enter an optional label, then press enter. Leave empty to skip.")
        self._text_entry("Optional label")

    def _submit_label(self, value: str) -> None:
        try:
            label = label_or_error(value, self._services.max_label_length)
        except ValueError as error:
            self._set_status(str(error))
            return
        self._selection = replace(self._selection, label=label)
        self._show_review()

    async def _resolve_review(self, key: str) -> None:
        if key == _BACK:
            self._show_profiles()
        elif key == _CANCEL:
            self._selection = LaunchSelection()
            self._show_projects()
        elif key == "launch":
            await self._launch()

    async def _choose_area(self, area: str) -> None:
        if area == _CANCEL:
            self._show_projects()
            return
        self._area = area
        self._step = Step.NAME
        self._set_status(f"Enter the new project name for {area}, then press enter.")
        self._text_entry("New project name")

    def _submit_name(self, value: str) -> None:
        """Validate the typed name, then review it; nothing is created on this keystroke."""
        if self._area is None:
            self._show_projects()
            return
        try:
            ProjectIdentity(area=self._area, name=value.strip())
        except ValueError as error:
            self._set_status(str(error))
            return
        self._name = value.strip()
        self._show_project_review()

    async def _resolve_project_review(self, key: str) -> None:
        if key in {_BACK, _CANCEL} or self._area is None or self._name is None:
            if key == _BACK:
                await self._show_areas()
            else:
                self._show_projects()
            return
        if key != "create":
            return
        self._busy = True
        try:
            command = CreateProjectCommand(self._area, self._name)
            created = await self._in_thread(
                lambda: self._services.creator.create(command), group="create-project"
            )
        except Exception as error:
            _LOG.exception("project creation failed")
            self._show_project_review()
            self._set_status(
                f"Project not created: {error}\nArea: {self._area}\nName: {self._name}"
            )
            return
        finally:
            self._busy = False
        await self.action_refresh()
        self._set_status(f"Created {created.identity}. Choose a project.")

    # Actions -------------------------------------------------------------------

    async def action_back(self) -> None:
        if self._busy:
            return
        if self._step in {Step.RESUME_PROJECTS, Step.RESUME_PROFILES}:
            self._show_projects()
        elif self._step is Step.RESUME_CONVERSATIONS:
            await self._show_resume_profiles()
        elif self._step is Step.RESUME_CONFIRM:
            await self._show_resume_conversations()
        elif self._step is Step.INSPECT:
            self._hide_output()
            if self._detail_id is not None:
                await self._show_detail(self._detail_id)
        elif self._step in {Step.FORCE_CONFIRM, Step.REMOTE_CONTROL_CONFIRM}:
            if self._detail_id is not None:
                await self._show_detail(self._detail_id)
        elif self._step is Step.SESSION_DETAIL:
            await self._show_sessions()
        elif self._step in {Step.PROFILES, Step.AREAS, Step.SESSIONS}:
            self._show_projects()
        elif self._step is Step.REVIEW:
            self._show_profiles()
        elif self._step is Step.PROJECT_REVIEW:
            await self._show_areas()
        elif self._step in _TEXT_STEPS:
            self._show_projects()

    async def action_refresh(self) -> None:
        """Re-read the catalogue, so a project another process created becomes selectable."""
        if self._busy:
            return
        try:
            self._catalogue = await self._in_thread(
                self._services.refresh_catalogue, group="catalogue"
            )
        except Exception:
            _LOG.exception("catalogue refresh failed")
            self._set_status("The project catalogue could not be re-read. Check this host.")
            return
        self._show_projects()

    async def action_add_project(self) -> None:
        if not self._busy:
            await self._show_areas()

    async def action_sessions(self) -> None:
        """Show every managed session, including ones this process never launched."""
        if self._busy:
            return
        await self._show_sessions()

    async def _show_sessions(self) -> None:
        """Re-read readiness, then list what the shared store actually holds.

        Readiness is refreshed first for the same reason the bot does it: a launch that
        failed here may have become ready since, and listing a stale FAILED would send the
        owner to fix something that already works.
        """
        self._step = Step.SESSIONS
        self._hide_entry()
        try:
            records = await self._load_sessions()
        except Exception as error:
            self._report_store_failure(error)
            return
        if not records:
            self._fill(())
            self._set_status(
                "There are no managed sessions. Press escape to return to the project list."
            )
            return
        self._set_status(f"{len(records)} managed session(s). Select one for detail.")
        self._fill(tuple((str(record.session_id), _session_row(record)) for record in records))

    async def _show_detail(self, session_value: str) -> None:
        """Show one session's state and what it means, re-read from the shared store.

        The record is looked up again rather than trusted from the list: the store has two
        writers, so a session can be stopped elsewhere while this list is on screen.
        """
        self._step = Step.SESSION_DETAIL
        self._hide_entry()
        try:
            record = await self._current_record(session_value)
        except Exception as error:
            self._report_store_failure(error)
            return
        if record is None:
            self._detail_id = None
            self._fill(((_BACK, "Back"),))
            self._set_status("That session is no longer available.")
            return
        self._detail_id = session_value
        self._set_status(
            f"{record.display.rendered}\nState: {record.state.value}\n{explain_state(record.state)}"
        )
        self._fill(self._detail_entries(record))

    async def _resolve_detail(self, key: str) -> None:
        if key == _BACK:
            await self._show_sessions()
        elif key == "attach":
            await self._show_attach()
        elif key == "inspect":
            await self._show_inspect()
        elif key == "remote-control":
            await self._confirm_remote_control()
        elif key == FORCE:
            await self._confirm_force()
        elif key in _ACTION_LABELS and key != FORCE:
            # The `key != FORCE` is redundant with the branch above and deliberately kept:
            # FORCE is a member of _ACTION_LABELS, so without it the only thing stopping a
            # single keypress from force-stopping is the *order* of these two branches.
            # Restructuring this chain into a dispatch table would silently remove the
            # confirmation step, and no existing test asserts the ordering itself.
            await self._stop(key)

    async def _show_inspect(self) -> None:
        """Render this session's captured output, sanitized by the shared port.

        `ports/terminal_text.sanitize_terminal_text` is the shared safety transformation,
        so nothing is re-implemented here. What is deliberately *not* reused is the
        Telegram presentation wrapper: its 4096-UTF-16-unit inline cap and
        session-output.txt attachment fallback exist because Telegram messages are bounded,
        and a scrollable local pane is not.
        """
        capture = self._services.capture
        if capture is None or self._detail_id is None:
            return
        record = await self._current_record(self._detail_id)
        if record is None:
            self._set_status("That session is no longer available.")
            return
        try:
            captured = await capture(record.session_id)
        except Exception as error:
            _LOG.exception("capture failed")
            self._set_status(
                f"{record.display.rendered}\nThe output could not be captured: {error}"
            )
            return
        self._step = Step.INSPECT
        self._hide_entry()
        self._fill(())
        raw = captured.encode()
        if b"\x00" in raw:
            # Matching the bot's refusal, for the same reason: a pane emitting NUL is not
            # rendering text, and printing it to a terminal can corrupt the display.
            text = "This session's output is binary and cannot be displayed."
        else:
            text = sanitize_terminal_text(
                raw,
                max_lines=_INSPECT_MAX_LINES,
                max_bytes=_INSPECT_MAX_BYTES,
                redactions=self._services.capture_redactions,
            )
        self._set_status(f"{record.display.rendered}\nOutput. Press escape to go back.")
        self._show_output(text or "This session has produced no output yet.")

    def _show_output(self, text: str) -> None:
        self.query_one("#output-pane").display = True
        self.query_one("#choices").display = False
        self.query_one("#output", Static).update(text)

    def _hide_output(self) -> None:
        self.query_one("#output-pane").display = False
        self.query_one("#choices").display = True

    # Resume ---------------------------------------------------------------------

    async def action_resume(self) -> None:
        """Open the resume flow, if this host wired a conversation service at all."""
        if self._busy or self._services.conversations is None:
            return
        self._resume_project = None
        self._resume_profile = None
        self._resume_choice = None
        self._step = Step.RESUME_PROJECTS
        self._hide_entry()
        self._set_status("Resume a conversation. Choose its project.")
        self._fill(
            tuple(
                (project.opaque_id, f"{project.area}/{project.name}") for project in self._catalogue
            )
            or ((_CANCEL, "No projects available"),)
        )

    async def _resolve_resume_project(self, key: str) -> None:
        project = next((item for item in self._catalogue if item.opaque_id == key), None)
        if project is None:
            # Stay in the resume flow, as the launch picker does for the same failure,
            # rather than dropping the owner into a different wizard with no explanation.
            self._set_status("That project is no longer available. Refresh and try again.")
            return
        self._resume_project = project
        # Guarded across the await for the reason Stage 3 established: a second entry point
        # firing mid-navigation used to reset the chosen project, after which selecting a
        # profile silently did nothing and only Escape recovered.
        self._busy = True
        try:
            await self._show_resume_profiles()
        finally:
            self._busy = False

    async def _show_resume_profiles(self) -> None:
        """Offer only profiles that report themselves resume-capable (DEC-002).

        Capability comes from `capabilities()`, which reports what each provider can
        actually do on this host — never from a version allowlist.
        """
        conversations = self._services.conversations
        if conversations is None:
            return
        try:
            capabilities = await conversations.capabilities()
        except Exception as error:
            _LOG.exception("resume capabilities failed")
            self._set_status(f"Resume is unavailable: {error}")
            self._fill(((_BACK, "Back"),))
            return
        capable = tuple(
            capability
            for capability in capabilities
            if capability.catalogue_available and capability.selected_resume_available
        )
        self._step = Step.RESUME_PROFILES
        if not capable:
            self._set_status("No agent on this host can resume a saved conversation.")
            self._fill(((_BACK, "Back"),))
            return
        self._set_status("Choose the agent whose conversation you want to resume.")
        self._fill(
            tuple((str(item.profile_id), str(item.profile_id)) for item in capable)
            + ((_BACK, "Back"),)
        )

    async def _resolve_resume_profile(self, key: str) -> None:
        if key == _BACK:
            await self.action_resume()
            return
        if not any(profile.profile_id == key for profile in self._services.profiles):
            # Defence in depth, matching the launch picker: the rows here are already
            # filtered to resume-capable profiles, so a key naming another one is stale.
            self._set_status("That agent is not available on this host.")
            return
        self._resume_profile = key
        self._resume_page = 1
        self._busy = True
        try:
            await self._show_resume_conversations()
        finally:
            self._busy = False

    async def _show_resume_conversations(self) -> None:
        """One bounded page of safe metadata; provider IDs never leave the server."""
        conversations = self._services.conversations
        if conversations is None or self._resume_profile is None or self._resume_project is None:
            return
        try:
            page = await conversations.catalogue(
                ConversationCatalogueQuery(
                    profile_id=ProfileId(self._resume_profile),
                    project_id=ProjectId(self._resume_project.opaque_id),
                    page=self._resume_page,
                    page_size=_RESUME_PAGE_SIZE,
                )
            )
        except Exception as error:
            _LOG.exception("conversation catalogue failed")
            self._set_status(f"The conversations could not be listed: {error}")
            self._fill(((_BACK, "Back"),))
            return
        self._step = Step.RESUME_CONVERSATIONS
        self._resume_page_count = page.page_count
        if page.unavailable_reason is not None:
            self._set_status(f"Conversations are unavailable: {page.unavailable_reason}")
            self._fill(((_BACK, "Back"),))
            return
        if not page.conversations:
            self._set_status("There are no saved conversations for that agent and project.")
            self._fill(((_BACK, "Back"),))
            return
        entries = [(str(item.reference), _conversation_row(item)) for item in page.conversations]
        if page.page > 1:
            entries.append((_PREVIOUS, "Previous page"))
        if page.page < page.page_count:
            entries.append((_NEXT, "Next page"))
        entries.append((_BACK, "Back"))
        self._set_status(f"Choose a conversation. Page {page.page} of {page.page_count}.")
        self._fill(tuple(entries))

    async def _resolve_resume_conversation(self, key: str) -> None:
        conversations = self._services.conversations
        if conversations is None:
            return
        if key == _BACK:
            await self._show_resume_profiles()
            return
        if key in {_NEXT, _PREVIOUS}:
            step = 1 if key == _NEXT else -1
            self._resume_page = max(1, min(self._resume_page + step, self._resume_page_count))
            self._busy = True
            try:
                await self._show_resume_conversations()
            finally:
                self._busy = False
            return
        try:
            # The reference is only ever one this surface rendered from a server-issued
            # page; constructing it here re-validates its opaque shape, and resolution is
            # server-side, so a forged or stale value resolves to nothing rather than a path.
            resolved = await conversations.resolve_for_resume(ConversationReference(key))
        except ValueError:
            self._set_status("That conversation selection is not valid.")
            return
        except Exception as error:
            _LOG.exception("conversation resolve failed")
            self._set_status(f"That conversation could not be resolved: {error}")
            return
        if resolved is None:
            self._set_status("That conversation is no longer available.")
            return
        self._resume_choice = resolved
        self._step = Step.RESUME_CONFIRM
        self._set_status(
            f"Resume {_conversation_row(resolved.summary)}\n"
            f"Agent: {self._resume_profile}\n"
            "This starts a new managed session continuing that conversation."
        )
        self._fill(((_CANCEL, "Cancel"), ("resume-confirm", "Resume it")))

    async def _resolve_resume_confirm(self, key: str) -> None:
        if key != "resume-confirm" or self._resume_choice is None:
            await self.action_resume()
            return
        if self._resume_project is None or self._resume_profile is None:
            await self.action_resume()
            return
        self._busy = True
        try:
            record = await self._services.launcher.resume(
                ResumeCommand(
                    ProjectId(self._resume_project.opaque_id),
                    ProfileId(self._resume_profile),
                    self._resume_choice,
                    _idempotency_key(),
                )
            )
        except Exception as error:
            _LOG.exception("resume failed")
            self._fill(((_BACK, "Back"),))
            self._set_status(f"The conversation was not resumed: {error}")
            return
        finally:
            self._busy = False
        if record.state is SessionState.FAILED:
            self._fill(((_BACK, "Back"),))
            self._set_status(
                "The resumed session did not become ready, but its pane may still exist. "
                "Reach it with:\n"
                f"{' '.join(self._services.attach_argv(str(record.session_id)))}"
            )
            return
        session_id = str(record.session_id)
        self.exit(AttachRequest(session_id, self._services.attach_argv(session_id)))

    async def _confirm_remote_control(self) -> None:
        """Ask before changing a live pane's control mode, re-checking the policy first."""
        if self._detail_id is None:
            return
        record = await self._current_record(self._detail_id)
        if record is None:
            self._set_status("That session is no longer available.")
            return
        if not remote_control_available(record):
            self._set_status(
                f"{record.display.rendered}\n"
                "Remote Control is not available for this session.\n"
                f"{explain_state(record.state)}"
            )
            return
        self._step = Step.REMOTE_CONTROL_CONFIRM
        self._hide_entry()
        self._set_status(
            f"Claude Remote Control for {record.display.rendered}\n"
            "Enabling lets this session be driven remotely; disabling returns it to local "
            "control only."
        )
        self._fill(
            (
                (_CANCEL, "Cancel"),
                ("remote-control-active", "Enable Remote Control"),
                ("remote-control-inactive", "Disable Remote Control"),
            )
        )

    async def _resolve_remote_control(self, key: str) -> None:
        desired = {
            "remote-control-active": RemoteControlState.ACTIVE,
            "remote-control-inactive": RemoteControlState.INACTIVE,
        }.get(key)
        if desired is None or self._detail_id is None:
            if self._detail_id is not None:
                await self._show_detail(self._detail_id)
            return
        self._busy = True
        try:
            record = await self._current_record(self._detail_id)
            if record is None:
                self._set_status("That session is no longer available.")
                return
            if not remote_control_available(record):
                self._set_status("Remote Control is no longer available for this session.")
                return
            state = await self._services.launcher.set_remote_control(
                RemoteControlCommand(record.session_id, desired, _idempotency_key())
            )
        except Exception as error:
            _LOG.exception("remote control failed")
            # Same reason as the failed stop: do not leave the cursor resting on the
            # button that just failed, or a second enter re-issues it as a blind retry.
            self._fill(((_BACK, "Back"),))
            self._set_status(
                f"Remote Control was not changed: {error}\n"
                "Go back and open the session again to see its current state."
            )
            return
        else:
            # Held for the same reason as `_stop`'s refresh: nothing else may run until
            # the result is on screen. These calls are synchronous today, so the window
            # is empty — the guard is here so it stays empty if one of them ever awaits.
            self._step = Step.SESSION_DETAIL
            self._set_status(f"{record.display.rendered}\nRemote Control: {state.value}")
            self._fill(self._detail_entries(record))
        finally:
            self._busy = False

    async def _confirm_force(self) -> None:
        """Ask a second time, on its own step, with abort as the resting choice.

        Force kills a running agent and cannot be undone, so it is deliberately not
        reachable by repeating whatever keystroke opened the detail: the abort entry is
        first and highlighted, and confirming means moving to a different row on purpose.
        """
        if self._detail_id is None:
            return
        record = await self._current_record(self._detail_id)
        if record is None:
            self._set_status("That session is no longer available.")
            return
        self._step = Step.FORCE_CONFIRM
        self._hide_entry()
        self._set_status(
            f"Force stop {record.display.rendered}?\n"
            "This kills the agent immediately and cannot be undone. Any work it has not "
            "saved is lost.\n"
            f"{explain_state(record.state)}"
        )
        self._fill(((_CANCEL, "Cancel"), ("force-confirm", "Yes, force stop it")))

    async def _resolve_force_confirm(self, key: str) -> None:
        if key == "force-confirm":
            await self._stop(FORCE)
            return
        # Anything else -- cancel, back, or an unrecognized key -- aborts without issuing.
        if self._detail_id is not None:
            await self._show_detail(self._detail_id)

    async def _stop(self, action: str) -> None:
        """Issue one stop, after re-reading the record and re-checking the policy.

        The policy is consulted again here rather than trusted from the rendered entry: the
        session may have moved on since the list was drawn, and an action that was legal
        then can be illegal now. The service would refuse it anyway — this keeps the owner
        from seeing an exception instead of an explanation.
        """
        if self._detail_id is None or self._busy:
            return
        self._busy = True
        try:
            record = await self._current_record(self._detail_id)
            if record is None:
                self._set_status("That session is no longer available.")
                return
            if action not in available_actions(record.state):
                self._set_status(
                    f"{record.display.rendered}\n"
                    f"{_ACTION_LABELS[action]} is no longer available for this session.\n"
                    f"{explain_state(record.state)}"
                )
                return
            await self._issue_stop(action, record)
        except Exception as error:
            _LOG.exception("stop failed")
            # Move the cursor off the confirm button before reporting. A failed force
            # leaves the owner resting on "Yes, force stop it", so without this a second
            # enter re-issues the kill as a retry nobody deliberately chose.
            self._fill(((_BACK, "Back"),))
            self._set_status(
                f"{_ACTION_LABELS[action]} did not complete: {error}\n"
                "The session was left as it is. Go back and open it again to see its "
                "current state, then retry if you still want to."
            )
            return
        else:
            # Inside the guard on purpose. `_busy` means "no other action may run until
            # this one's result is on screen" — and `_show_detail` awaits, so releasing
            # first leaves a window where the step has flipped but the list still holds
            # the previous screen's entries.
            await self._show_detail(self._detail_id)
        finally:
            self._busy = False

    async def _issue_stop(self, action: str, record: SessionRecord) -> None:
        """Send exactly one curated command; the commands themselves carry no arguments."""
        launcher = self._services.launcher
        if action == GRACEFUL:
            await launcher.graceful_stop(GracefulStopCommand(record.session_id, record.profile_id))
        elif action == CLEANUP:
            await launcher.cleanup(CleanupCommand(record.session_id))
        else:
            await launcher.force_stop(ForceStopCommand(record.session_id))

    async def _show_attach(self) -> None:
        """Render the command that reaches this pane, or say why there is none.

        The affordance is always offered and answers when chosen, rather than being hidden
        when unavailable. Hiding it is what the bot does, and it leaves the owner unable to
        tell a dead pane from a surface that simply forgot to draw the button.
        """
        if self._detail_id is None:
            return
        try:
            record = await self._current_record(self._detail_id)
            if record is None:
                self._set_status("That session is no longer available.")
                return
            command = await self._services.launcher.copy_attach(record.session_id)
        except Exception as error:
            self._report_store_failure(error)
            return
        if command is None:
            self._set_status(
                f"{record.display.rendered}\n"
                "Attach is not available: this session's pane is not live, or the pane "
                "found for it belongs to a different project or agent.\n"
                f"{explain_state(record.state)}"
            )
            return
        self._set_status(f"{record.display.rendered}\nAttach with:\n{command}")

    def _detail_entries(self, record: SessionRecord) -> tuple[tuple[str, str], ...]:
        """The actions this session offers, taken from the policy and not decided here.

        The stop entries are exactly `available_actions(record.state)` in the order it
        returns them, which puts the destructive one last. Adding, filtering, or reordering
        here is what `tests/contract/test_session_actions_parity.py` exists to catch.
        """
        entries: list[tuple[str, str]] = [("attach", "Copy attach")]
        if self._services.capture is not None:
            entries.append(("inspect", "Inspect output"))
        if remote_control_available(record):
            entries.append(("remote-control", "Claude Remote Control"))
        entries.extend(
            (action, _ACTION_LABELS[action]) for action in available_actions(record.state)
        )
        entries.append((_BACK, "Back"))
        return tuple(entries)

    def _report_store_failure(self, error: Exception) -> None:
        """Report a failed store or terminal read without tearing the surface down.

        Every read this screen makes can fail: the store has a second writer, and a
        recovery surface is used precisely when things are already broken. Losing the app
        to an exception is the one outcome that leaves the owner with nothing.
        """
        _LOG.exception("session read failed", exc_info=error)
        self._fill(((_BACK, "Back"),))
        self._set_status(
            f"The managed sessions could not be read: {error}\n"
            "Press escape to return to the project list."
        )

    async def _current_record(self, session_value: str) -> SessionRecord | None:
        """Re-read one session from the store, without refreshing readiness.

        The re-read is what detects a session stopped by the other writer while this list
        was on screen. The *refresh* is deliberately not repeated: it rescans every record
        and runs a tmux capture per FAILED session, so repeating it on each navigation
        would make opening a detail and copying its attach command cost three full passes.
        The bot refreshes once per list open for the same reason.
        """
        for record in await self._read_sessions():
            if str(record.session_id) == session_value:
                return record
        return None

    async def _load_sessions(self) -> tuple[SessionRecord, ...]:
        """Refresh readiness, then return the sessions worth showing.

        Order is whatever the store returns; nothing here sorts, and the row's age column
        is what tells the owner how old a session is.
        """
        await self._services.launcher.refresh_readiness()
        return await self._read_sessions()

    async def _read_sessions(self) -> tuple[SessionRecord, ...]:
        """List the store's sessions, filtering what no surface can act on."""
        records = await self._services.launcher.list_sessions()
        # ENDED is filtered exactly as the bot filters it: the record is retained for audit
        # but there is nothing left to reach, inspect, or stop.
        return tuple(record for record in records if record.state is not SessionState.ENDED)

    async def _launch(self) -> None:
        project, profile = self._selection.project, self._selection.profile
        if project is None or profile is None:
            self._show_projects()
            return
        self._busy = True
        self._set_status("Launching…")
        try:
            record = await self._services.launcher.launch(
                LaunchCommand(
                    ProjectId(project.opaque_id),
                    ProfileId(profile.profile_id),
                    _idempotency_key(),
                    self._selection.label,
                )
            )
        except Exception as error:
            _LOG.exception("launch failed")
            self._show_review()
            self._set_status(f"The session was not started: {error}\n{self._selection.review()}")
            return
        finally:
            self._busy = False
        if record.state is SessionState.FAILED:
            self._show_review()
            self._set_status(
                "The session did not become ready, but its pane may still exist. Reach it "
                "with:\n"
                f"{' '.join(self._services.attach_argv(str(record.session_id)))}\n"
                "Check this host before retrying, or a second session will run alongside it.\n"
                f"{self._selection.review()}"
            )
            return
        session_id = str(record.session_id)
        self.exit(AttachRequest(session_id, self._services.attach_argv(session_id)))


def _conversation_row(summary: ConversationSummary) -> str:
    """Safe selection metadata only — never a provider ID, path, or path fragment."""
    described = summary.description or "(no description)"
    return f"{described} · {summary.state.value} · {_age(summary.updated_at)}"


def _age(created_at: datetime) -> str:
    minutes = max(0, int((datetime.now(UTC) - created_at).total_seconds() // 60))
    return f"{minutes}m ago"


def _session_row(record: SessionRecord) -> str:
    return f"{record.display.rendered} · {record.state.value} · {_age(record.created_at)}"


def _idempotency_key() -> str:
    from uuid import uuid4

    return f"tui-{uuid4()}"


def run_local_terminal(
    context: TuiContext,
    *,
    runner: Callable[[TuiContext], AttachRequest | None] | None = None,
) -> AttachRequest | None:
    """Run the terminal surface and return what the caller must attach to, if anything."""
    return runner(context) if runner is not None else RemoteAgentsTui(context).run()
