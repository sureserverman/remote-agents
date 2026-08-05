"""Local terminal surface mirroring the bot wizard, then attaching to what it started."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static

from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.errors import ProjectCreationError
from remote_agents.application.project_admin import CreateProjectCommand
from remote_agents.application.project_catalog import CatalogProject, search_catalogue
from remote_agents.domain.models import ProfileId, ProjectId, SessionState
from remote_agents.domain.projects import ProjectIdentity


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
    command: str


def label_or_error(value: str, limit: int) -> str | None:
    """Normalize an optional session label under the configured bound."""
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) > limit or any(not character.isprintable() for character in normalized):
        raise ValueError(f"use a visible label of up to {limit} characters")
    return normalized


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
        self._step = "projects"
        self._area: str | None = None
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

    def _fill(self, entries: tuple[tuple[str, str], ...]) -> None:
        choices = self.query_one("#choices", ListView)
        choices.clear()
        for key, text in entries:
            item = ListItem(Label(text))
            item.entry_key = key
            choices.append(item)

    def _show_projects(self, query: str = "") -> None:
        self._step = "projects"
        projects = search_catalogue(self._catalogue, query) if query else self._catalogue
        self._set_status(f"Choose a project. {len(projects)} available.")
        self._fill(
            tuple(
                (project.opaque_id, f"{project.area}/{project.name}  [{project.group}]")
                for project in projects
            )
        )

    def _show_profiles(self) -> None:
        self._step = "profiles"
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
        self._step = "review"
        self._set_status(f"Review\n{self._selection.review()}")
        self._fill((("launch", "Launch"), ("back", "Back"), ("cancel", "Cancel")))

    def _show_areas(self) -> None:
        self._step = "areas"
        areas = self._services.creator.available_areas()
        if not areas:
            self._set_status("No area is available for a new project.")
            self._fill((("cancel", "Back"),))
            return
        self._set_status("Choose the area for the new project.")
        self._fill(tuple((area, area) for area in areas) + (("cancel", "Back"),))

    # Interaction ---------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._step == "projects":
            self._show_projects(event.value)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._step == "name":
            await self._submit_name(event.value)
        elif self._step == "label":
            self._submit_label(event.value)

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        key = getattr(event.item, "entry_key", None)
        if key is None:
            return
        if self._step == "projects":
            self._choose_project(key)
        elif self._step == "profiles":
            self._choose_profile(key)
        elif self._step == "review":
            await self._resolve_review(key)
        elif self._step == "areas":
            self._choose_area(key)

    def _choose_project(self, opaque_id: str) -> None:
        project = next(
            (item for item in self._catalogue if item.opaque_id == opaque_id),
            None,
        )
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
        self._step = "label"
        self._set_status("Enter an optional label, or submit empty to skip.")
        self._fill(())
        filter_input = self.query_one("#filter", Input)
        filter_input.value = ""
        filter_input.placeholder = "Optional label"
        filter_input.focus()

    def _submit_label(self, value: str) -> None:
        try:
            label = label_or_error(value, self._services.max_label_length)
        except ValueError as error:
            self._set_status(str(error))
            return
        self._selection = replace(self._selection, label=label)
        self._show_review()

    async def _resolve_review(self, key: str) -> None:
        if key == "back":
            self._show_profiles()
        elif key == "cancel":
            self._selection = LaunchSelection()
            self._show_projects()
        elif key == "launch":
            await self._launch()

    def _choose_area(self, area: str) -> None:
        if area == "cancel":
            self._show_projects()
            return
        self._area = area
        self._step = "name"
        self._set_status(f"Enter the new project name for {area}.")
        self._fill(())
        filter_input = self.query_one("#filter", Input)
        filter_input.value = ""
        filter_input.placeholder = "New project name"
        filter_input.focus()

    async def _submit_name(self, value: str) -> None:
        if self._area is None:
            self._show_projects()
            return
        try:
            ProjectIdentity(area=self._area, name=value.strip())
        except ValueError as error:
            self._set_status(str(error))
            return
        try:
            created = await asyncio.to_thread(
                self._services.creator.create, CreateProjectCommand(self._area, value.strip())
            )
        except ProjectCreationError as error:
            self._set_status(f"Project not created: {error}")
            return
        self.action_refresh()
        self._set_status(f"Created {created.identity}. Choose a project.")

    # Actions -------------------------------------------------------------------

    def action_back(self) -> None:
        if self._step in {"profiles", "areas"}:
            self._show_projects()
        elif self._step == "review":
            self._show_profiles()
        elif self._step in {"label", "name"}:
            self._restore_filter()
            self._show_projects()

    def action_refresh(self) -> None:
        """Re-read the catalogue, so a project another process created becomes selectable."""
        self._catalogue = self._services.refresh_catalogue()
        self._restore_filter()
        self._show_projects()

    def action_add_project(self) -> None:
        self._show_areas()

    def _restore_filter(self) -> None:
        filter_input = self.query_one("#filter", Input)
        filter_input.value = ""
        filter_input.placeholder = "Filter projects"

    async def _launch(self) -> None:
        project, profile = self._selection.project, self._selection.profile
        if project is None or profile is None:
            self._show_projects()
            return
        self._set_status("Launching…")
        record = await self._services.launcher.launch(
            LaunchCommand(
                ProjectId(project.opaque_id),
                ProfileId(profile.profile_id),
                _idempotency_key(),
                self._selection.label,
            )
        )
        if record.state is SessionState.FAILED:
            self._show_review()
            self._set_status(
                "The session did not become ready. Check this host, then retry.\n"
                f"{self._selection.review()}"
            )
            return
        session_id = str(record.session_id)
        self.exit(AttachRequest(session_id, self._services.attach_command(session_id)))


def _idempotency_key() -> str:
    from uuid import uuid4

    return f"tui-{uuid4()}"


def run_local_terminal(
    context: TuiContext,
    *,
    runner: Callable[[TuiContext], AttachRequest | None] | None = None,
) -> int:
    """Run the terminal surface and report whether it completed without an error."""
    result = runner(context) if runner is not None else RemoteAgentsTui(context).run()
    if result is None:
        return 0
    print(result.command)
    return 0
