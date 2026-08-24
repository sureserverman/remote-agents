"""The launch wizard: pick a project, pick an agent, review it, go.

Three screens replacing three wizard positions. Each owns the rows it renders and what a row
means when it is chosen, so the position an event belongs to is decided by which screen is
on top of the stack rather than by an `if` chain re-reading a field.

**It was four, and the step it lost was the optional label.** A name chosen before the launch
is chosen before there is anything to look at, and nothing could change it afterwards — so the
surface asked for a name at the one moment the owner knew least, and then never asked again.
Naming a session now lives on the session, as the detail's Rename row, which is also where the
bot has always done it; `telegram/service.py` records the same decision for the same reason.
`label_or_error` and `LabelWithinBound` outlived the step and belong to that entry now.

**One back-path shortcut is deliberately gone**, and this is where the pair that `project.py`
and `resume.py` both refer back to was removed. Back at the review used to jump straight to the
agent list; on a real stack it is one level. Its sibling — Escape at the label jumping past the
agent choice to the project list — left with the label screen itself rather than being fixed.
"""

from __future__ import annotations

from dataclasses import replace

from textual import events
from textual.binding import Binding
from textual.timer import Timer
from textual.widgets import Input, OptionList

from remote_agents.adapters.tui.model import _BACK, _CANCEL, LaunchSelection
from remote_agents.adapters.tui.preferences import ALPHABETICAL, RECENCY
from remote_agents.adapters.tui.screens.base import (
    NEVER_EMPTY,
    ChoiceScreen,
)
from remote_agents.application.project_catalog import search_catalogue

#: What the status line says about each order. The sentence names the order the list is in
#: *and* the key, because the key is the only way to discover it and the footer's "Reorder"
#: does not say what the other order would be.
_ORDER_SENTENCE = {
    RECENCY: "most recently used first. Type to filter, ctrl+t for alphabetical.",
    ALPHABETICAL: "in alphabetical order. Type to filter, ctrl+t for most recently used.",
}

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

    BINDINGS = [
        # `ctrl+t` rather than a bare letter: `render_projects` focuses the filter, so this
        # pane holds the keyboard in an `Input` by construction and a plain `t` would be
        # *typed* rather than bound. `ctrl+o` was the other instinct and is already Resume.
        #
        # A screen binding, not an app-level one: it means something only where a project
        # list is drawn, and `ChoiceScreen.check_action` answers only for the app's six.
        Binding("ctrl+t", "toggle_project_order", "Reorder"),
    ]

    async def populate(self) -> None:
        # Before the first draw, not after it. `ChoiceScreen.on_mount` awaits this method and
        # renders nothing itself, so the ordering the app applies here is in place for the
        # very first list the owner sees -- see `RemoteAgentsTui.ensure_catalogue_ordered`
        # for why the app's own `on_mount` is the wrong hook. Idempotent, so the screens that
        # subclass this one do not each re-order the same snapshot.
        await self.tui.ensure_catalogue_ordered()
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
        # Consume any search the last keystroke scheduled. Without this, Ctrl+R pressed inside
        # the debounce window leaves that timer to fire after this render and run one more
        # `search_catalogue` over the same query — the duplicate work the debounce exists to
        # remove, reintroduced by the key whose whole job is to re-read once.
        self._flush_filter()
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
        # The active order is named in words and the region takes no severity: DEC-010 is
        # explicit that colour is a second signal and never the only one, and an order is a
        # fact about the list rather than a condition to report.
        order = _ORDER_SENTENCE[self.tui.project_order]
        self.set_status(f"Choose a project — {len(projects)} available, {order}")
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

    async def action_toggle_project_order(self) -> None:
        """Swap the order, keep the query, and say which order is now in force.

        `keep_focus=True` for the reason Refresh was corrected by: this key does not leave the
        position, so it has no business discarding what the owner has typed in the filter --
        or moving the keyboard out of it.
        """
        query = self.query_one("#filter", Input).value
        await self.tui.switch_project_order()
        self.render_projects(query, keep_focus=True)

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
        already stale. What DEC-008 actually forbids is Textual's cancel-on-re-entry worker
        mode — the `exclusive` flag on a `@work` group. So this is a `Timer`: the behaviour
        the decision permits, without the worker mode it does not.

        **Corrected at Task 2.1's Tier-1 review.** This paragraph used to claim the decision
        "enforces that with an unconditional grep for the literal flag assignment", and that
        the sentence had been written around the token to avoid tripping that sweep. **No such
        check exists** — searched across `tests/architecture/` and the whole tree, the flag
        appears only in prose like this and in `run_worker`/`@work` calls that never pass it.
        The claim was also self-refuting, since the sentence making it names the token twice.
        DEC-008 is held by review and by comments like this one, which is a weaker guarantee
        than the old wording promised and is the one actually in force.
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

    def key_down(self, event: events.Key) -> None:
        """Down-arrow leaves the filter for the rows, which enter alone used to be able to do.

        `Input` binds no `down`, so the key bubbles from the focused entry to this screen and
        this handler is what it reaches. Guarded on the entry actually holding the keyboard:
        once focus is on the list, `OptionList` consumes `down` as cursor movement and never
        gets here.
        """
        entry = self.query_one("#filter", Input)
        if not entry.has_focus:
            return
        event.stop()
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
        # wizard, so an agent left over from an abandoned pass must not survive it.
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
        """The project this wizard is launching into — unless the trail already names it.

        The chooser now stands between the project list and this screen, and its crumb is
        the project; repeating it here would make the trail say the same thing twice in a
        row, which is the exact defect the label step's crumb was once removed for. So this
        yields the project only when nothing beneath it already did — which today means the
        legacy path where this screen is pushed without a chooser (tests, and any future
        direct entry).
        """
        from remote_agents.adapters.tui.screens.dashboard import ProjectChooserScreen

        stack = self.app.screen_stack
        index = stack.index(self) if self in stack else -1
        if index >= 1 and isinstance(stack[index - 1], ProjectChooserScreen):
            return ""
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
                    else f"{profile.profile_id}  (unavailable: {profile.blocked_reason})",
                )
                for profile in self.services.profiles
            )
        )

    async def choose(self, key: str) -> None:
        profile = next((item for item in self.services.profiles if item.profile_id == key), None)
        if profile is None or not profile.available:
            reason = profile.blocked_reason if profile is not None else "unknown profile"
            self.announce(f"That agent cannot be launched here: {reason}", severity="warning")
            return
        self.tui.selection = replace(self.tui.selection, profile=profile)
        await self.advance_to(ReviewScreen())


class ReviewScreen(ChoiceScreen):
    """The last position before a launch is issued, resting on Back rather than Launch.

    **It was a `GatheredSelectionScreen` and no longer needs to be, because the work it was
    protecting was the label.** That base class exists for a review position holding "a whole
    flow's worth of choices... one keystroke from being discarded with no way back to them", and
    the load-bearing half of that is *no way back*: this screen held the gathered selection plus
    a typed label, and walking back cleared the entry on the way in, so the label was genuinely
    unrecoverable. What is left is two list selections, and escape lands on the agent list with
    both lists still standing — two keystrokes to re-pick, which is the same reasoning that has
    always exempted the project filter from the flow-jump protection.

    `ProjectReviewScreen` keeps the base class, and that asymmetry is the point rather than an
    oversight: it holds a *typed* project name that `NameScreen.populate` also clears, so its
    work is still work escape cannot give back. `GatheredSelectionScreen` therefore has one
    subclass, which is one more than none — the class stays because the screen that needs it
    needs it, not because two screens once shared it.
    """

    #: Launch, Back and Cancel are written here.
    empty_state = NEVER_EMPTY

    position = "REVIEW"

    @property
    def crumb(self) -> str:
        """The agent chosen a screen ago.

        **It read "Review", and that became wrong the moment the label step was removed.**
        The label step's own crumb was the profile id, and its docstring recorded exactly why:
        named for its own step instead, "it read fine and lost the agent from the trail
        entirely, since no later position carried it." Review was that later position. So
        deleting the step reintroduced the defect its predecessor had already been fixed for,
        and the trail at the commit point read `Projects › infra/existing › Review` — the
        project, and no sign of which agent was about to be launched into it.

        Naming the agent here is also what the convention already asked for: a crumb names *the
        choice that led here* when there was one, and the step's own name when there was not.
        There was not, when the label sat in between; there is now. What the position *is* goes
        to the status line under the same region split — the trail carries what the owner chose,
        the status carries what going through with it does.
        """
        profile = self.tui.selection.profile
        return profile.profile_id if profile is not None else "Review"

    async def populate(self) -> None:
        self.hide_entry()
        self.render_review()

    def render_review(self) -> None:
        # The project and the agent are both in the breadcrumb, so this line does not repeat
        # them. It named the label — the one part of the selection the trail could not carry —
        # and once that step was removed it rendered `Label: none` unconditionally, which is a
        # line whose whole content is the absence of a step that no longer exists.
        #
        # What it says instead is the consequence, because this is the position that commits to
        # it and the consequence is the largest the surface has: from a bare shell a ready
        # launch execs away, replacing this process with the tmux client; hosted by a client
        # on the project's own server it switches that client instead and this app stays. The
        # status line below covers both — "hands this terminal to the session's pane" is what
        # each route does — and the routing itself lives in `adapters/tui/attach.py` and the
        # README's attach paragraph.
        #
        # **"or prints how to reach it" is not hedging; it is the other half of the truth.**
        # The first version of this line stopped at "hands this terminal to the session's pane"
        # and reasoned carefully about only one of the ways that can fail to happen — a launch
        # that never reaches readiness. `attach_to` has two more, both inside *ready*: with
        # `TMUX` set it refuses to nest and prints the command instead (`attach.py`), and an
        # `execvp` that raises does the same. Running the surface from inside tmux is a path
        # `docs/operator-runbook.md` treats as ordinary, so that branch is not exotic. Both a
        # Tier-2 review and a gate evaluator found the overclaim independently.
        #
        # Deliberately *not* branched on `os.environ["TMUX"]` at render time, which would be
        # more precise and is the obvious alternative: this string is captured in `REVIEW.svg`,
        # and a status that depends on the environment would make that baseline differ for a
        # developer who runs the suite inside tmux — the exact class of flake
        # `test_tui_snapshots.py` pins `TEXTUAL_THEME` and `NO_COLOR` against. One sentence true
        # in every case beats two that are each true in one and make the net non-deterministic.
        #
        # No severity, per DEC-010 — this is an instruction about what is about to happen, not a
        # report of a condition, and that entry is explicit that a status carries a severity
        # only when its words do.
        self.set_status(
            "A ready launch hands this terminal to the session's pane, or prints how to reach it."
        )
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
                # Deliberately *not* severity-coloured, and this is the closest call of the
                # five sites considered — a reviewer flagged it as one a later reader may
                # reasonably reopen. `failure.status` is the half the owner may still need in
                # a minute: the attach command, or where to go next. Some values of it do
                # report a condition ("Nothing was started"), which is the argument for
                # colouring it; against that, the *why* already goes to a toast under the
                # region split sub-plan 3 built, and colouring a string whose content varies
                # would mean the same region is sometimes an explanation and sometimes an
                # instruction, in the same colour. Left neutral until the split itself is
                # revisited, rather than decided one call site at a time.
                self.set_status(failure.status)
                self.announce(failure.explanation)
