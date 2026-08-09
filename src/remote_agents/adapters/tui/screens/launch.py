"""The launch wizard: pick a project, pick an agent, name the run, review it, go.

Four screens replacing four `Step` members. Each owns the rows it renders and what a row
means when it is chosen, so the position an event belongs to is decided by which screen is
on top of the stack rather than by an `if` chain re-reading a field.
"""

from __future__ import annotations

from dataclasses import replace

from textual.widgets import Input, OptionList

from remote_agents.adapters.tui.model import _BACK, _CANCEL, LaunchSelection, label_or_error
from remote_agents.adapters.tui.screens.base import ChoiceScreen
from remote_agents.application.project_catalog import search_catalogue


class ProjectsScreen(ChoiceScreen):
    """The resting position of the whole surface, and the bottom of the screen stack.

    It is the app's default screen rather than a pushed one, which is what makes "the stack
    can never empty" structural instead of a rule every back path has to remember.
    """

    position = "PROJECTS"
    filter_placeholder = "Filter projects"

    async def populate(self) -> None:
        self.render_projects()

    def render_projects(self, query: str = "", *, keep_focus: bool = False) -> None:
        """Draw the catalogue, filtered by whatever is typed in the filter input."""
        catalogue = self.tui.catalogue
        projects = search_catalogue(catalogue, query) if query else catalogue
        entry = self.query_one("#filter", Input)
        entry.display = True
        entry.placeholder = "Filter projects"
        self.set_status(
            f"Choose a project. {len(projects)} available. "
            "Type to filter, then press enter for the list."
        )
        self.show_choices(
            tuple(
                (project.opaque_id, f"{project.area}/{project.name}  [{project.group}]")
                for project in projects
            ),
            focus=False,
        )
        if not keep_focus:
            entry.value = ""
            entry.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self.render_projects(event.value, keep_focus=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Move from the filter into the filtered list, so arrows and enter can pick."""
        event.stop()
        choices = self.query_one("#choices", OptionList)
        if choices.options:
            choices.highlighted = 0
            choices.focus()

    async def choose(self, key: str) -> None:
        project = next((item for item in self.tui.catalogue if item.opaque_id == key), None)
        if project is None:
            self.set_status("That project is no longer available. Refresh and try again.")
            return
        # A fresh selection rather than a patched one: choosing a project restarts the
        # wizard, so an agent or label left over from an abandoned pass must not survive it.
        self.tui.selection = replace(LaunchSelection(), project=project)
        await self.app.push_screen(ProfilesScreen())


class ProfilesScreen(ChoiceScreen):
    """The curated agents, each named with the reason it cannot be launched here."""

    position = "PROFILES"
    status = "Choose an agent."

    async def populate(self) -> None:
        self.hide_entry()
        self.show_choices(
            tuple(
                (
                    profile.profile_id,
                    profile.profile_id
                    if profile.available
                    else f"{profile.profile_id}  (unavailable: {profile.reason})",
                )
                for profile in self.services.profiles
            )
        )

    async def choose(self, key: str) -> None:
        profile = next((item for item in self.services.profiles if item.profile_id == key), None)
        if profile is None or not profile.available:
            reason = profile.reason if profile is not None else "unknown profile"
            self.set_status(f"That agent cannot be launched here: {reason}")
            return
        self.tui.selection = replace(self.tui.selection, profile=profile)
        await self.app.push_screen(LabelScreen())


class LabelScreen(ChoiceScreen):
    """One optional free-text label, bounded by the configured length."""

    position = "LABEL"
    status = "Enter an optional label, then press enter. Leave empty to skip."
    filter_placeholder = "Optional label"

    async def populate(self) -> None:
        self.text_entry("Optional label")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.submit(event.value)

    def submit(self, value: str) -> None:
        try:
            label = label_or_error(value, self.services.max_label_length)
        except ValueError as error:
            self.set_status(str(error))
            return
        self.tui.selection = replace(self.tui.selection, label=label)
        # Not awaited, unlike its siblings above, because `on_input_submitted` is synchronous
        # — Textual mounts the pushed screen on the next pump cycle either way, and awaiting
        # only decides whether *this* caller waits for the mount. Nothing here touches the new
        # screen's widgets afterwards, so there is nothing to wait for.
        self.app.push_screen(ReviewScreen())


class ReviewScreen(ChoiceScreen):
    """The last position before a launch is issued, resting on Back rather than Launch."""

    position = "REVIEW"

    async def populate(self) -> None:
        self.hide_entry()
        self.render_review()

    def render_review(self) -> None:
        self.set_status(f"Review\n{self.tui.selection.review()}")
        self.show_choices((("launch", "Launch"), (_BACK, "Back"), (_CANCEL, "Cancel")), highlight=1)

    async def choose(self, key: str) -> None:
        if key == _BACK:
            await self.tui.go_back()
        elif key == _CANCEL:
            self.tui.selection = LaunchSelection()
            self.tui.return_to_projects()
        elif key == "launch":
            failure = await self.tui.launch()
            if failure is not None:
                # Re-render before reporting, so the cursor leaves "Launch" and a second
                # enter cannot re-issue a launch nobody deliberately chose.
                self.render_review()
                self.set_status(failure)
