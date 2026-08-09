"""Creating a project: choose its area, name it, review it, then create it.

Three screens replacing three `Step` members. The area and the name used to live on the app
as `_area` and `_name` — two of the seven navigation fields — and are constructor arguments
here, so a screen cannot be reached without the state it needs and cannot read a value some
other flow left behind.

**Two back-path shortcuts are deliberately gone**, the same pair the launch wizard lost in
Task 2.1 and for the same reason. Escape at the name entry used to jump straight to the
project list, skipping the area choice, because the name entry was grouped with the launch
label as a text position; Back at the review used to jump straight to the area list,
skipping the name. On a real stack each is
one level, so the owner who mistypes a name can now correct it instead of restarting the
flow. No affordance changes and every position stays reachable — the depth does.
`test_back_out_of_the_add_project_flow_stops_at_every_position` is that behaviour.
"""

from __future__ import annotations

import logging

from textual.widgets import Input

from remote_agents.adapters.tui.model import _BACK, _CANCEL, selectable_area
from remote_agents.adapters.tui.screens.base import ChoiceScreen
from remote_agents.application.project_admin import CreateProjectCommand
from remote_agents.domain.projects import ProjectIdentity

_LOG = logging.getLogger(__name__)


class AreasScreen(ChoiceScreen):
    """The areas of the development root a new project may be created in."""

    position = "AREAS"

    async def populate(self) -> None:
        self.hide_entry()
        try:
            offered = await self.tui.in_thread(self.services.creator.available_areas, group="areas")
        except Exception:
            _LOG.exception("listing areas failed")
            self.set_status("The development root could not be read. Check this host.")
            self.show_choices(((_CANCEL, "Back"),))
            return
        areas = tuple(area for area in offered if selectable_area(area))
        if not areas:
            self.set_status("No area is available for a new project.")
            self.show_choices(((_CANCEL, "Back"),))
            return
        self.set_status("Choose the area for the new project.")
        self.show_choices(tuple((area, area) for area in areas) + ((_CANCEL, "Back"),))

    async def choose(self, key: str) -> None:
        if key == _CANCEL:
            self.tui.return_to_projects()
            return
        await self.advance_to(NameScreen(key))


class NameScreen(ChoiceScreen):
    """The typed name for the new project. Nothing is created on this keystroke."""

    position = "NAME"
    filter_placeholder = "New project name"

    def __init__(self, area: str) -> None:
        super().__init__()
        self.area = area

    async def populate(self) -> None:
        self.set_status(f"Enter the new project name for {self.area}, then press enter.")
        self.text_entry("New project name")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.submit(event.value)

    def submit(self, value: str) -> None:
        """Validate the typed name, then review it; nothing is created on this keystroke."""
        try:
            ProjectIdentity(area=self.area, name=value.strip())
        except ValueError as error:
            self.set_status(str(error))
            return
        self.app.push_screen(ProjectReviewScreen(self.area, value.strip()))


class ProjectReviewScreen(ChoiceScreen):
    """Name the project before creating it, exactly as the bot's Review does."""

    position = "PROJECT_REVIEW"

    def __init__(self, area: str, project_name: str) -> None:
        super().__init__()
        self.area = area
        # `project_name`, not `name`: Textual's `DOMNode` already defines `name` as a
        # read-only property, so assigning it raises rather than shadowing it.
        self.project_name = project_name

    async def populate(self) -> None:
        self.hide_entry()
        self.render_review()

    def render_review(self) -> None:
        self.set_status(f"Review new project\nArea: {self.area}\nName: {self.project_name}")
        self.show_choices(
            (("create", "Create"), (_BACK, "Back"), (_CANCEL, "Cancel")), highlight=1
        )

    async def choose(self, key: str) -> None:
        if key == _BACK:
            await self.tui.go_back()
            return
        if key == _CANCEL:
            self.tui.return_to_projects()
            return
        if key != "create":
            return
        tui = self.tui
        tui.set_busy(True)
        try:
            command = CreateProjectCommand(self.area, self.project_name)
            created = await tui.in_thread(
                lambda: self.services.creator.create(command), group="create-project"
            )
        except Exception as error:
            _LOG.exception("project creation failed")
            # Re-render before reporting, so the cursor leaves "Create" and a second enter
            # cannot re-issue a creation nobody deliberately chose.
            self.render_review()
            self.set_status(
                f"Project not created: {error}\nArea: {self.area}\nName: {self.project_name}"
            )
            return
        finally:
            tui.set_busy(False)
        await tui.action_refresh()
        tui.body.set_status(f"Created {created.identity}. Choose a project.")
