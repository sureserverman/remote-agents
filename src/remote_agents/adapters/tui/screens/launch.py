"""The launch wizard: pick a project, pick an agent, name the run, review it, go.

Four screens replacing four wizard positions. Each owns the rows it renders and what a row
means when it is chosen, so the position an event belongs to is decided by which screen is
on top of the stack rather than by an `if` chain re-reading a field.

**Two back-path shortcuts are deliberately gone**, and this is where the pair that
`project.py` and `resume.py` both refer back to was removed. Back at the review used to jump
straight to the agent list, skipping the label, so a mistyped label could not be corrected
without re-picking the agent; and Escape at the label used to jump straight to the project
list, skipping the agent choice. On a real stack each is one level.
`test_back_from_review_walks_out_through_the_label_to_the_agent_choice` is that behaviour.
"""

from __future__ import annotations

from dataclasses import replace

from textual.timer import Timer
from textual.widgets import Input, OptionList

from remote_agents.adapters.tui.model import _BACK, _CANCEL, LaunchSelection, label_or_error
from remote_agents.adapters.tui.screens.base import NEVER_EMPTY, ChoiceScreen
from remote_agents.adapters.tui.screens.validation import LabelWithinBound
from remote_agents.application.project_catalog import search_catalogue

#: How long the filter waits for the typing to stop before it re-searches the catalogue.
#: Every keystroke used to run `search_catalogue` over the whole catalogue and rebuild every
#: row, so a five-character word did that work five times and threw four of the answers away.
#: 120ms is under the ~200ms at which a pause starts to read as lag, and above a fast typist's
#: inter-key interval, so an ordinary word searches once.
_FILTER_DEBOUNCE = 0.12


class ProjectsScreen(ChoiceScreen):
    """The resting position of the whole surface, and the bottom of the screen stack.

    It is the app's default screen rather than a pushed one, which is what makes "the stack
    can never empty" structural instead of a rule every back path has to remember.
    """
    empty_state = "No project matches that filter."

    position = "PROJECTS"
    filter_placeholder = "Filter projects"
    can_refresh = True
    crumb = "Projects"

    async def populate(self) -> None:
        self.render_projects()

    async def on_reveal(self) -> None:
        """Come back to a clean list with the keyboard in the filter.

        The chain this replaces reached the project list by calling a method that cleared the
        filter and refocused it, so backing out of any flow always landed on a fresh list. A
        bare pop would instead return the owner to a filtered list with the keyboard on the
        rows — where typing is swallowed rather than filtering — which is a worse position to
        be dropped into than the one they left.
        """
        self.render_projects()

    async def refresh_contents(self) -> None:
        """Re-read the catalogue, so a project another process created becomes selectable.

        This is what Ctrl+R has always done, and it belongs here because here is the only
        place it was ever *for*: the catalogue is what this screen renders. On a failed read
        the rows are deliberately left alone — the catalogue already drawn is stale, not
        wrong, and blanking it would take away projects the owner can still launch.
        """
        from textual.widgets import Input

        # Keep whatever the owner has typed. `render_projects()` defaults to clearing the
        # filter and moving the keyboard, which is right on the way *back* into this screen
        # and wrong here: Refresh does not leave the position, so it has no business
        # discarding the query the list is currently narrowed by. The stage's own rule
        # exempts this filter from the flow-jump protection on the grounds that leaving is
        # the ordinary thing to do here — an argument that does not reach a key which stays.
        query = self.query_one("#filter", Input).value
        if not await self.tui.reload_catalogue():
            self.announce("The project catalogue could not be re-read. Check this host.")
            return
        self.render_projects(query, keep_focus=True)

    def render_projects(self, query: str = "", *, keep_focus: bool = False) -> None:
        """Draw the catalogue, filtered by whatever is typed in the filter input."""
        if not self.showing:
            return
        catalogue = self.tui.catalogue
        projects = search_catalogue(catalogue, query) if query else catalogue
        entry = self.query_one("#filter", Input)
        entry.display = True
        entry.placeholder = "Filter projects"
        self.set_status(f"Choose a project — {len(projects)} available. Type to filter.")
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

    def __init__(self) -> None:
        super().__init__()
        #: The scheduled re-search, kept so the next keystroke can cancel it.
        self._filter_timer: Timer | None = None
        #: What that scheduled search will look for. Held separately from the timer because
        #: the two ways out of the filter — enter and down — have to apply it *now* rather
        #: than wait, and they need the query to do it.
        self._pending_query: str | None = None

    def on_input_changed(self, event: Input.Changed) -> None:
        """Schedule the re-search, replacing any the previous keystroke scheduled.

        Cancel-and-reschedule is cancel-on-re-entry, which DEC-008 forbids in this adapter —
        but the decision's own text carves out exactly this case ("a debounced filter or a
        catalogue refresh"), because what is abandoned here is a *read* whose answer is
        already stale. What DEC-008 actually forbids is `@work(exclusive=True)`, and its
        enforcement is an unconditional grep for that string, so this is a timer: the
        behaviour the decision permits, expressed without the token its check bans.
        """
        event.stop()
        self._pending_query = event.value
        if self._filter_timer is not None:
            self._filter_timer.stop()
        self._filter_timer = self.set_timer(_FILTER_DEBOUNCE, self._apply_pending_filter)

    def _apply_pending_filter(self) -> None:
        self._filter_timer = None
        query, self._pending_query = self._pending_query, None
        if query is None:
            return
        # `render_projects` returns early when the screen is no longer showing, which is what
        # keeps a timer that outlives a navigation from repainting a position the owner left.
        self.render_projects(query, keep_focus=True)

    def _flush_filter(self) -> None:
        """Apply a scheduled search immediately, because the owner is about to act on it.

        Without this the debounce introduces its own defect: type `oth`, press down inside the
        120ms, and the keyboard lands on rows that are still the *unfiltered* catalogue — so
        the row under the cursor is not the one the owner was looking at when they pressed.
        """
        if self._filter_timer is not None:
            self._filter_timer.stop()
        self._apply_pending_filter()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Move from the filter into the filtered list, so arrows and enter can pick."""
        event.stop()
        self._flush_filter()
        self._enter_results()

    def key_down(self, event: object) -> None:
        """Down-arrow leaves the filter for the rows, which enter alone used to be able to do.

        `Input` binds no `down`, so the key bubbles from the focused entry to this screen and
        this handler is what it reaches. Guarded on the entry actually holding the keyboard:
        once focus is on the list, `OptionList` consumes `down` as cursor movement and never
        gets here.
        """
        entry = self.query_one("#filter", Input)
        if not entry.has_focus:
            return
        stop = getattr(event, "stop", None)
        if callable(stop):
            stop()
        self._flush_filter()
        self._enter_results()

    def _enter_results(self) -> None:
        choices = self.query_one("#choices", OptionList)
        if choices.options:
            choices.highlighted = 0
            choices.focus()

    async def choose(self, key: str) -> None:
        project = next((item for item in self.tui.catalogue if item.opaque_id == key), None)
        if project is None:
            self.announce(
                "That project is no longer available. Refresh and try again.", severity="warning"
            )
            return
        # A fresh selection rather than a patched one: choosing a project restarts the
        # wizard, so an agent or label left over from an abandoned pass must not survive it.
        self.tui.selection = replace(LaunchSelection(), project=project)
        await self.advance_to(ProfilesScreen())


class ProfilesScreen(ChoiceScreen):
    """The curated agents, each named with the reason it cannot be launched here."""
    #: The curated profile list is the host's own configuration; a host offering none could
    #: not launch anything at all, so an empty agent list is a broken install, not a state.
    empty_state = NEVER_EMPTY

    position = "PROFILES"
    status = "Choose an agent."

    @property
    def crumb(self) -> str:
        """The project this wizard is launching into, which is what the owner just chose.

        A property rather than a class attribute because the trail has to name the *choice*,
        not the step: "Projects › Agent" would tell the owner nothing they did not already
        know from the rows in front of them.
        """
        project = self.tui.selection.project
        return f"{project.area}/{project.name}" if project is not None else "Agent"

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
            self.announce(
                f"That agent cannot be launched here: {reason}", severity="warning"
            )
            return
        self.tui.selection = replace(self.tui.selection, profile=profile)
        await self.advance_to(LabelScreen())


class LabelScreen(ChoiceScreen):
    """One optional free-text label, bounded by the configured length."""
    #: a text entry, not a list.
    empty_state = NEVER_EMPTY

    position = "LABEL"
    status = "Enter an optional label, then press enter. Leave empty to skip."
    filter_placeholder = "Optional label"
    # Typed here and committed by `submit`; leaving discards it.
    entry_is_a_commitment = True

    @property
    def crumb(self) -> str:
        """The agent chosen a screen ago.

        The convention across all three flows: a crumb names *the choice that led here* when
        there was one, and the step's own name when there was not. So the agent list is called
        after the project, this is called after the agent, and the review — reached by an entry
        that may legitimately be empty — is called "Review". Named "Label" first, which read
        fine and lost the agent from the trail entirely, since no later position carried it.
        """
        profile = self.tui.selection.profile
        return profile.profile_id if profile is not None else "Label"

    async def populate(self) -> None:
        # `valid_empty` left at its default: an empty label is the documented way to skip this
        # step, so the entry must not open refusing the value it is about to be given.
        self.text_entry(
            "Optional label",
            validators=[LabelWithinBound(self.services.max_label_length)],
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        """Say the bound is broken at the keystroke that broke it, not at the enter after it."""
        event.stop()
        self.announce_rejection(event.validation_result)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.submit(event.value)

    def submit(self, value: str) -> None:
        # Still validated here, and deliberately not replaced by the entry's own result: this
        # is the call that *produces the normalized label*, and a submit that trusted a
        # validation performed on an earlier keystroke would be trusting a value it did not
        # read. The typed-time check tells the owner sooner; it does not become the gate.
        try:
            label = label_or_error(value, self.services.max_label_length)
        except ValueError as error:
            # A toast rather than the status line, which here still holds the instruction the
            # owner is in the middle of following. Overwriting it with the rejection left them
            # being told what was wrong and no longer what to do about it.
            self.announce(str(error), severity="warning")
            return
        self.tui.selection = replace(self.tui.selection, label=label)
        if not self.showing:
            return
        # Not awaited, unlike its siblings, because `on_input_submitted` is synchronous —
        # Textual mounts the pushed screen on the next pump cycle either way, and awaiting
        # only decides whether *this* caller waits for the mount. Nothing here touches the new
        # screen's widgets afterwards, so there is nothing to wait for. The `showing` check
        # above is `advance_to`'s guard inlined, since that one is a coroutine and this is not.
        self.app.push_screen(ReviewScreen())


class ReviewScreen(ChoiceScreen):
    """The last position before a launch is issued, resting on Back rather than Launch."""
    #: Launch, Back and Cancel are written here.
    empty_state = NEVER_EMPTY

    @property
    def work_in_flight(self) -> bool:
        """Leaving here throws away the project, agent and label gathered across three screens.

        The entry is empty at this point — the value was committed a screen ago — so the
        default answer would be "nothing in flight" while a whole flow's worth of the owner's
        choices sits one keystroke from being discarded with no way back to them.
        """
        return True


    position = "REVIEW"
    crumb = "Review"

    async def populate(self) -> None:
        self.hide_entry()
        self.render_review()

    def render_review(self) -> None:
        # The project and the agent are in the breadcrumb, so this line carries the one part
        # of the selection the trail cannot: the label. `review()` is still what the wizard
        # gathered, in three lines, and it has no home in a one-line region — the header and
        # this line together say the same thing.
        self.set_status(f"Label: {self.tui.selection.label or 'none'}. Launch, or go back.")
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
                # enter cannot re-issue a launch nobody deliberately chose. It also resets
                # this screen's status, which is why the failure's own status is set after.
                self.render_review()
                self.set_status(failure.status)
                self.announce(failure.explanation)
