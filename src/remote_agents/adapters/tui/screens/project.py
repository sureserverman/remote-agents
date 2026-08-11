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
from remote_agents.adapters.tui.screens.base import NEVER_EMPTY, ChoiceScreen
from remote_agents.adapters.tui.screens.validation import NameIsAProjectIdentity
from remote_agents.application.project_admin import CreateProjectCommand
from remote_agents.domain.projects import ProjectIdentity

_LOG = logging.getLogger(__name__)


class AreasScreen(ChoiceScreen):
    """The areas of the development root a new project may be created in."""

    empty_state = "No area in the development root can hold a new project."

    position = "AREAS"
    crumb = "New project"

    async def populate(self) -> None:
        self.hide_entry()
        try:
            offered = await self.tui.in_thread(self.services.creator.available_areas, group="areas")
        except Exception as error:
            _LOG.exception("listing areas failed")
            # States the failure rather than pointing at the exit, for the reason recorded on
            # `report_store_failure`: the toast carrying the why expires, and this region is
            # what is left.
            self.set_status(
                "The development root could not be read. Press escape to return to the "
                "project list.",
                severity="error",
            )
            self.announce(f"The development root could not be read: {error}")
            # Through `entries`, unlike the sibling branch six lines below, and deliberately:
            # this is a *failed read*, not an empty one. Routing it through `trailing=` would
            # substitute the declared empty state — "No area in the development root can hold
            # a new project" — which states as fact the very thing this branch could not
            # determine. An error is not an emptiness.
            self.show_choices(((_CANCEL, "Back"),))
            return
        areas = tuple(area for area in offered if selectable_area(area))
        if not areas:
            self.set_status("No area is available for a new project.")
            # Through `trailing`, not `entries`: a list holding only a Back button is a blank
            # pane as far as the owner is concerned, and passing Back as data is exactly how a
            # screen would opt out of the empty state it just declared.
            self.show_choices((), trailing=((_CANCEL, "Back"),))
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

    #: a text entry, not a list.
    empty_state = NEVER_EMPTY

    position = "NAME"
    filter_placeholder = "New project name"
    # Typed here and committed by `submit`; leaving discards it.
    entry_is_a_commitment = True

    def __init__(self, area: str) -> None:
        super().__init__()
        self.area = area

    @property
    def crumb(self) -> str:
        """The area chosen a screen ago, which is what the name is being given inside."""
        return self.area

    async def populate(self) -> None:
        self.set_status("Enter the new project name, then press enter.")
        # `valid_empty=False` unlike the label: a project has to be called something, so an
        # empty entry here is a value that will be refused rather than a step being skipped.
        self.text_entry(
            "New project name",
            validators=[NameIsAProjectIdentity(self.area)],
            valid_empty=False,
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        """Reject an unusable name while it is being typed, in the domain's own words."""
        event.stop()
        self.announce_rejection(event.validation_result)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.submit(event.value)

    def submit(self, value: str) -> None:
        """Validate the typed name, then review it; nothing is created on this keystroke."""
        try:
            ProjectIdentity(area=self.area, name=value.strip())
        except ValueError as error:
            # As on the label entry: the rejection is a toast so the instruction the owner is
            # following stays where they were reading it.
            self.announce(str(error), severity="warning")
            return
        if not self.showing:
            return
        # `advance_to`'s guard inlined: that one is a coroutine and this handler is not.
        self.app.push_screen(ProjectReviewScreen(self.area, value.strip()))


class ProjectReviewScreen(ChoiceScreen):
    """Name the project before creating it, exactly as the bot's Review does."""

    #: Create, Back and Cancel are written here.
    empty_state = NEVER_EMPTY

    @property
    def work_in_flight(self) -> bool:
        """Leaving here throws away the area and the project name gathered across two screens.

        The entry is empty at this point — the value was committed a screen ago — so the
        default answer would be "nothing in flight" while a whole flow's worth of the owner's
        choices sits one keystroke from being discarded with no way back to them.
        """
        return True

    position = "PROJECT_REVIEW"
    crumb = "Review"

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
        # The area is in the breadcrumb; this line names what will be created and nothing the
        # header already said.
        self.set_status(f"Create {self.area}/{self.project_name}?")
        self.show_choices((("create", "Create"), (_BACK, "Back"), (_CANCEL, "Cancel")), highlight=1)

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
        # Both awaited calls are inside both windows, and the catalogue re-read was outside
        # both. It runs a development-root scan on a worker thread, so it yields to the pump —
        # and between the guard's release and this line the review screen was fully interactive
        # again with its Create row still drawn, so a second enter re-issued a creation for a
        # project that had just been made. Nothing was destroyed by it (the second create
        # raises "already exists" and is reported), but it is the same await-then-render window
        # the rest of this surface closes everywhere else. Found by the Task 2.2 review while
        # sweeping for awaited calls with no affordance; it predates this task.
        async with self.holding_the_guard(), self.awaiting("Creating the project…"):
            try:
                command = CreateProjectCommand(self.area, self.project_name)
                created = await tui.in_thread(
                    lambda: self.services.creator.create(command), group="create-project"
                )
            except Exception as error:
                _LOG.exception("project creation failed")
                # Re-render before reporting, so the cursor leaves "Create" and a second enter
                # cannot re-issue a creation nobody deliberately chose. Re-rendering restores
                # this screen's own status, which is what the owner needs left behind — the
                # failure itself is the toast. It runs inside `awaiting`, whose exit puts the
                # pre-command line back over it, so the order is: render, then report, then
                # restore — and the restore writes the same line the render just did.
                self.render_review()
                self.announce(f"Project not created: {error}")
                return
            # Spelled out rather than delegated to `action_refresh`, which used to do exactly
            # this pair and no longer does: refresh is the active screen's own re-read now and
            # does not navigate, whereas creating a project genuinely does end at the project
            # list. This is the only caller that wanted the navigation, so it is the one that
            # keeps it.
            if not await tui.reload_catalogue():
                self.render_review()
                # `warning`, not `error`: the project exists. Reporting a partial success in
                # the same red as a creation that failed is how an owner learns to retry
                # something that already worked.
                self.announce(
                    f"Created {created.identity}, but the project catalogue could not be "
                    "re-read. Check this host, then refresh the project list.",
                    severity="warning",
                )
                return
        tui.return_to_projects()
        # Announced rather than written onto the project list's status line, which is that
        # screen's own instruction and is rewritten by its next render anyway — so the
        # confirmation used to survive exactly until the owner typed one character into the
        # filter. A toast outlives the redraw and belongs to the action, not to the position.
        tui.announce(f"Created {created.identity}.", severity="information")
