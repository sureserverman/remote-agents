"""Local terminal surface mirroring the bot wizard, then attaching to what it started."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static

from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.project_admin import CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject, search_catalogue
from remote_agents.domain.models import ProfileId, ProjectId, SessionState
from remote_agents.domain.projects import ProjectIdentity

_LOG = logging.getLogger(__name__)


class Step(StrEnum):
    """The wizard positions, each deciding which handler may act on an event."""

    PROJECTS = "projects"
    PROFILES = "profiles"
    LABEL = "label"
    REVIEW = "review"
    AREAS = "areas"
    NAME = "name"
    PROJECT_REVIEW = "project-review"


_TEXT_STEPS = frozenset({Step.LABEL, Step.NAME})
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
    ListView { height: 1fr; }
    """
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("ctrl+n", "add_project", "Add project"),
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
        self._status = "Choose a project."

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield Static(self._status, id="status")
            yield Input(placeholder="Filter projects", id="filter")
            yield ListView(id="choices")
        yield Footer()

    def on_mount(self) -> None:
        self._show_projects()

    # Rendering -----------------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self._status = text
        self.query_one("#status", Static).update(text)

    def _fill(
        self, entries: tuple[tuple[str, str], ...], *, focus: bool = True, highlight: int = 0
    ) -> None:
        """Render the choices, and take the keyboard only when the list is the next decision.

        Refilling while the owner is typing a filter must leave the keyboard where it is, or
        every character after the first lands on the list instead of the query.
        """
        choices = self.query_one("#choices", ListView)
        choices.clear()
        for key, text in entries:
            item = ListItem(Label(text))
            item.entry_key = key
            choices.append(item)
        if entries and focus:
            choices.index = min(highlight, len(entries) - 1)
            choices.focus()

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
            offered = await asyncio.to_thread(self._services.creator.available_areas)
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
        choices = self.query_one("#choices", ListView)
        if choices.children:
            choices.index = 0
            choices.focus()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        key = getattr(event.item, "entry_key", None)
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
            created = await asyncio.to_thread(
                self._services.creator.create, CreateProjectCommand(self._area, self._name)
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
        if self._step in {Step.PROFILES, Step.AREAS}:
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
            self._catalogue = await asyncio.to_thread(self._services.refresh_catalogue)
        except Exception:
            _LOG.exception("catalogue refresh failed")
            self._set_status("The project catalogue could not be re-read. Check this host.")
            return
        self._show_projects()

    async def action_add_project(self) -> None:
        if not self._busy:
            await self._show_areas()

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
                "The session did not become ready. Check this host, then retry.\n"
                f"{self._selection.review()}"
            )
            return
        session_id = str(record.session_id)
        self.exit(AttachRequest(session_id, self._services.attach_argv(session_id)))


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
