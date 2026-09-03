"""The launch wizard: pick a project, pick an agent, go.

Two screens for two wizard positions. Each owns the rows it renders and what a row means when
it is chosen, so the position an event belongs to is decided by which screen is on top of the
stack rather than by an `if` chain re-reading a field.

**It was four, and it lost two steps, one per plan.** The optional label went first: a name
chosen before the launch is chosen before there is anything to look at, and nothing could
change it afterwards — so the surface asked for a name at the one moment the owner knew least,
and then never asked again. Naming a session now lives on the session, as the detail's Rename
row, which is also where the bot has always done it; `telegram/service.py` records the same
decision for the same reason. `label_or_error` and `LabelWithinBound` outlived the step and
belong to that entry now.

**Then the review step went, and losing the label is why it could.** That position existed to
protect a typed label from an escape that would clear it. Without one it held two list
selections, re-pickable in two keystrokes, and guarded nothing at all — while the resume flow,
which commits to the same kind of act, had no equivalent position. DEC-033 says a step the
other surface does not have is a step to remove. `ProfilesScreen` inherited what actually
belonged to the *act* rather than to the position: the sentence naming the consequence, the
resting cursor that stops a repeated keypress committing, and the failure handling.

**`ProjectReviewScreen` in `project.py` is a different screen and stays**, and that asymmetry
is deliberate rather than an oversight: it holds a *typed* project name that `NameScreen.populate`
also clears, so its work is still work escape cannot give back. It is therefore the one
remaining subclass of `GatheredSelectionScreen` — the class stays because the screen that needs
it needs it, not because two screens once shared it.

**One back-path shortcut is deliberately gone**, and this is where the pair that `project.py`
and `resume.py` both refer back to was removed. Back at the review used to jump straight to the
agent list; on a real stack it is one level. Its sibling — Escape at the label jumping past the
agent choice to the project list — left with the label screen itself rather than being fixed.
Both subjects have now outlived the positions they were about, which is why this paragraph
names them in the past tense and no code enforces either.
"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from textual import events
from textual.binding import Binding
from textual.timer import Timer
from textual.widgets import Input, OptionList

from remote_agents.adapters.tui.model import _BACK, LaunchSelection
from remote_agents.adapters.tui.preferences import ALPHABETICAL, RECENCY
from remote_agents.adapters.tui.rows import project_row_content
from remote_agents.adapters.tui.screens.base import (
    NEVER_EMPTY,
    ChoiceScreen,
)
from remote_agents.application.project_catalog import search_catalogue

#: What the status line says about each order, on the positions that describe the list in
#: their status (the dashboard puts the sessions' counts there instead). The sentence names the
#: order the list is in *and* the key, because the key is the only way to discover it.
_ORDER_SENTENCE = {
    RECENCY: "most recently used first. / filters, o for alphabetical.",
    ALPHABETICAL: "in alphabetical order. / filters, o for most recently used.",
}

#: The pane title's hint for each order -- `Projects · recent first · o toggles order`.
_ORDER_TITLE = {
    RECENCY: "recent first",
    ALPHABETICAL: "a–z",
}

PROJECTS_HINT = "enter choose · / filter · o order"
"""The keymap under the status on the projects positions."""

PROJECTS_PLACEHOLDER = "/ filter projects"

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
    filter_placeholder = PROJECTS_PLACEHOLDER
    can_refresh = True
    crumb = "Projects"

    DEFAULT_CSS = """
    ProjectsScreen #choices {
        border: round $secondary; text-wrap: nowrap; text-overflow: ellipsis;
    }
    """

    BINDINGS = [
        # Bare letters, which the redesign made affordable here: the keyboard now rests on the
        # *rows*, and `/` is how the owner reaches the filter -- so `o` is a key, not a typed
        # character, until they have pressed `/`, after which `Input` takes every printable key
        # first and `o` is once more a letter. `ctrl+t` stays as the hidden alias that works
        # from inside the filter too. Screen bindings, not app-level ones: they mean something
        # only where a project list is drawn. Hidden from the footer; the pane title carries
        # them, which is where they are true.
        Binding("slash", "focus_filter", "filter", show=False),
        Binding("o", "toggle_project_order", "order", show=False),
        Binding("ctrl+t", "toggle_project_order", "order", show=False),
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
        """Draw the catalogue, filtered by whatever is typed in the filter input.

        **The keyboard rests on the rows, and `/` takes it to the filter** -- the redesign's
        arrangement, and a reversal of the one before it, which focused the filter on every
        render so typing filtered at once. That is what made every bare letter unavailable as a
        key here; with the rows holding the keyboard, `o` toggles the order and `/` is one
        keystroke from filtering, which is what the pane title and the hint row advertise.

        Two columns: the name, and the project's last-launch age against the right edge, muted,
        from the same usage read that ordered the list (`RemoteAgentsTui.project_last_used`).
        Laid out to the pane's measured width, so the rows are redrawn on resize.
        """
        if not self.showing:
            return
        catalogue = self.tui.catalogue
        projects = search_catalogue(catalogue, query) if query else catalogue
        entry = self.query_one("#filter", Input)
        entry.display = True
        entry.placeholder = PROJECTS_PLACEHOLDER
        choices = self.query_one("#choices", OptionList)
        choices.border_title = (
            f"Projects[$text-muted] · {_ORDER_TITLE[self.tui.project_order]} · o toggles order[/]"
        )
        width = choices.content_size.width or None
        last_used = self.tui.project_last_used
        self._describe_projects(len(projects))
        self.show_choices(
            tuple(
                (
                    project.opaque_id,
                    project_row_content(project.name, last_used.get(project.opaque_id), width),
                )
                for project in projects
            ),
            focus=not keep_focus,
        )
        if not keep_focus:
            entry.value = ""

    def _describe_projects(self, count: int) -> None:
        """The status and hint for a position whose whole content is the project list.

        The active order is named in words and the region takes no severity: DEC-010 is
        explicit that colour is a second signal and never the only one, and an order is a
        fact about the list rather than a condition to report. `DashboardScreen` overrides
        this to say nothing -- its status carries the sessions' counts.
        """
        order = _ORDER_SENTENCE[self.tui.project_order]
        self.set_status(f"Choose a project — {count} available, {order}", hint=PROJECTS_HINT)

    def action_focus_filter(self) -> None:
        """`/`: hand the keyboard to the filter. Enter, down or escape hand it back."""
        self.query_one("#filter", Input).focus()

    def key_escape(self, event: events.Key) -> None:
        """Escape inside the filter returns to the rows, keeping the query.

        Only while the entry holds the keyboard: everywhere else escape is the app's `back`,
        which this handler must not swallow. Guarded exactly as `key_down` is.
        """
        entry = self.query_one("#filter", Input)
        if not entry.has_focus:
            return
        event.stop()
        event.prevent_default()
        self._flush_filter()
        self._enter_results()

    def on_resize(self, event: events.Resize) -> None:
        """Lay the two columns out again for the new width, keeping query and keyboard."""
        if not self.showing:
            return
        found = self.query("#filter")
        if not found:
            return
        self.render_projects(found.first(Input).value, keep_focus=True)

    async def action_toggle_project_order(self) -> None:
        """Swap the order, keep the query, and say which order is now in force.

        `keep_focus=True` for the reason Refresh was corrected by: this key does not leave the
        position, so it has no business discarding what the owner has typed in the filter --
        or moving the keyboard out of it.
        """
        query = self.query_one("#filter", Input).value
        # Consume any search the last keystroke scheduled, for the reason `refresh_contents`
        # does: a timer armed inside the 120ms debounce would otherwise fire after this render
        # and re-search the same query, which is the duplicate work the debounce exists to
        # remove. Harmless here -- the redundant render would use the same query and the new
        # order -- but the two methods share one contract ("keep the query, do not move the
        # keyboard") and an asymmetry between them is how the two quietly diverge.
        self._flush_filter()
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
    """The curated agents, each named with the reason it cannot be launched here.

    **This is the launch flow's commit position**, and it became one when the review step
    between it and the launch was removed. That step guarded nothing: DEC-033 removed the
    label that gave it a reason to exist, and what was left was two list selections that
    escape gives back in two keystrokes. The resume flow has no equivalent position — the
    conversation list *is* its commit — which is the asymmetry DEC-033 says to resolve by
    removing the step the other surface does not have.

    Two things followed the act onto this screen rather than dying with the position that
    used to hold them, and both are load-bearing:

    - **The consequence is said before it happens.** A ready launch execs this process away
      (DEC-023), which is the largest thing this surface does, and the owner reads it on the
      screen that commits to it. The wording covers both routes because `attach_to` has more
      than one: with `TMUX` set it refuses to nest and prints the command instead, and an
      `execvp` that raises does the same.
    - **No repeated keypress commits here**, on either the success path or the failure path,
      and the two are guarded by different things. DEC-007's mitigation is why the list grows a
      Back row and rests on it (`render_profiles`); the app's `_leaving` flag is what refuses a
      second selection once a launch has succeeded. Neither covers a launch that *failed* —
      `_busy` is cleared before the FAILED return and `_leaving` is never set — so `choose`
      refuses any selection arriving while the cursor rests on nothing.
    """

    #: The curated profile list is the host's own configuration; a host offering none could
    #: not launch anything at all, so an empty agent list is a broken install, not a state.
    #: The Back row goes through `trailing` rather than `entries` for the reason
    #: `show_choices` gives: a list holding nothing but Back is empty in every sense the owner
    #: cares about, and passing it as data is how a screen silently opts out of its own empty
    #: state. `NEVER_EMPTY` is the honest answer here either way.
    empty_state = NEVER_EMPTY

    position = "PROFILES"
    #: No severity, per DEC-010: this is an instruction about what is about to happen, not a
    #: report of a condition. Inherited whole from the review position, whose own comment
    #: recorded that "or prints how to reach it" is not hedging but the other half of the
    #: truth — `docs/operator-runbook.md` treats running this surface inside tmux as ordinary.
    status = (
        "Choose an agent — a ready launch hands this terminal to the session's pane, "
        "or prints how to reach it."
    )

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
        self.render_profiles()

    def render_profiles(self, *, resting: bool = True) -> None:
        """Draw the agents, resting the cursor on the row that commits nothing.

        **The resting row is the whole of DEC-007's mitigation at this position**, and it
        arrived with the act. `ResumeConversationsScreen` went through the identical
        transformation when its own confirmation was removed, and its comment names the
        hazard in the words that apply here unchanged: two enters from the list before this
        one — one to arrive, one still queued — would otherwise start a session and exec this
        terminal away. The rule being applied is *no repeated keypress commits at a commit
        position*, not "review screens rest on Back"; whether the rows are data or actions
        does not enter into it.

        **The cost is one key**, and it depends on an upstream default worth naming: Down from
        the resting row reaches the first agent because Textual's `OptionList.action_cursor_down`
        goes through `find_next_enabled`, which *wraps*. `find_next_enabled_no_wrap` ships
        beside it, and were the widget ever to use that the first agent would be a full
        list-length away with nothing failing.

        `resting=False` — rest on **nothing** — is what the failure path asks for, and it is a
        different question from where the cursor rests on arrival. See `choose`.
        """
        entries = tuple(
            (
                profile.profile_id,
                profile.profile_id
                if profile.available
                else f"{profile.profile_id}  (unavailable: {profile.blocked_reason})",
            )
            for profile in self.services.profiles
        )
        self.show_choices(
            entries,
            highlight=len(entries) if resting else None,
            trailing=((_BACK, "Back"),),
        )

    def _resting_on_nothing(self) -> bool:
        """Whether the list is drawn with no row under the cursor.

        Read from the widget rather than tracked in a flag, so it cannot disagree with what is
        on screen. Answers `False` when the list has not been filled yet — a screen with no
        `#choices` yet has nothing queued against it either, and raising `NoMatches` out of a
        message handler would exit the app.
        """
        choices = self.query("#choices")
        if not choices:
            return False
        return cast(OptionList, choices.first()).highlighted is None

    async def choose(self, key: str) -> None:
        if key == _BACK:
            # Kept beside the central `_BACK` branch in
            # `ChoiceScreen.on_option_list_option_selected` rather than left to it, for the
            # reason that branch's own comment gives: `choose` is called directly, by tests
            # and by `after_command`, without passing through the handler at all.
            await self.tui.go_back()
            return
        if self._resting_on_nothing():
            # **A commit position commits the row the cursor is on, and nothing else.**
            #
            # This is what makes the failure re-render below load-bearing rather than
            # cosmetic. One terminal read can carry several Enters — key autorepeat, a double
            # tap, buffered stdin over a laggy link — and the driver parses them into key
            # events posted back to back, so every one of them is turned into its own
            # selection *before* the first handler runs. Each selection carries the agent row
            # it was built from, so moving the cursor does not disarm the ones already queued;
            # only refusing them here does.
            #
            # The success path never needed this: `_leave` sets `_leaving`, `busy` reads it,
            # and it is never cleared. The failure path has no such guard —
            # `RemoteAgentsTui.launch` clears `_busy` in a `finally` that runs *before* its
            # FAILED return — so the second of a queued pair met an open guard and launched a
            # second agent into the same project, with a different idempotency key, while the
            # owner was reading a toast about the first. Pinned by
            # `test_two_queued_enters_on_a_failed_launch_start_exactly_one_session`.
            #
            # Deliberately *not* fixed by holding `_busy` open: that flag means "a command is
            # in flight", and after a failed launch none is. The true statement is about the
            # cursor, so that is what is checked.
            return
        profile = next((item for item in self.services.profiles if item.profile_id == key), None)
        if profile is None or not profile.available:
            reason = profile.blocked_reason if profile is not None else "unknown profile"
            self.announce(f"That agent cannot be launched here: {reason}", severity="warning")
            return
        self.tui.selection = replace(self.tui.selection, profile=profile)
        failure = await self.tui.launch()
        if failure is None:
            return
        # Re-render before reporting, so the cursor leaves the agent row and a second enter
        # cannot re-issue a launch nobody deliberately chose. **The reasoning is inherited
        # from the review position and was never about it**: `RemoteAgentsTui.launch` clears
        # `_busy` in a `finally`, so the guard is open again the moment a failure returns.
        #
        # Rested on **nothing** rather than on Back, which is where arrival rests. Back is a
        # legal place for the cursor to sit and a terrible place to *move* it to: a queued
        # enter would then walk the owner out of the flow, away from a status line holding
        # the attach command they may need in a minute. `SessionsScreen._draw_listing` takes
        # the same option for the same reason — a list whose keys act on a live session may
        # not have its cursor moved by anything but the owner.
        #
        # **It does not reset the status**, and the predecessor's comment here said it did.
        # `render_review` called `set_status` as part of re-rendering; `render_profiles` does
        # not, and neither does `show_choices`. Harmless today — the failure's own status is
        # written on the next line either way — but it stated an ordering constraint that does
        # not exist, which is the kind of thing a later reorder trusts. Found by the Stage 1
        # gate's second review pass, which measured it rather than reading it.
        self.render_profiles(resting=False)
        # Deliberately *not* severity-coloured, and this is the closest call of the five sites
        # considered. `failure.status` is the half the owner may still need in a minute: the
        # attach command, or where to go next. Some values of it do report a condition
        # ("Nothing was started"), which is the argument for colouring it; against that, the
        # *why* already goes to a toast under the region split, and colouring a string whose
        # content varies would mean the same region is sometimes an explanation and sometimes
        # an instruction, in the same colour.
        self.set_status(failure.status)
        self.announce(failure.explanation)
