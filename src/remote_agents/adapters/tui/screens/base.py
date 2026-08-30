"""The body every choice screen renders, and the one fill path all of them share.

This is where `RemoteAgentsTui._fill` moved. Keeping a single choke point is not tidiness:
`show_choices` deduplicates row keys, and the Sub-plan 1 handoff records why that guard has
to live on the shared path rather than at the one call site that needs it today — a screen
fed by an external source must not be able to reintroduce the crash by forgetting a guard.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult, ScreenStackError
from textual.containers import Vertical, VerticalScroll
from textual.notifications import SeverityLevel
from textual.screen import Screen
from textual.validation import ValidationResult, Validator
from textual.widgets import Footer, Header, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from remote_agents.adapters.tui.model import _BACK, _EMPTY

if TYPE_CHECKING:
    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.context import TuiContext

_LOG = logging.getLogger(__name__)

#: The three app bindings that leave the current flow entirely — each unwinds the stack to the
#: resting position, which is what makes them able to discard a half-typed value.
_FLOW_JUMPS = frozenset({"add_project", "sessions", "resume"})

#: What a screen declares as its `empty_state` when it cannot legitimately be empty — its rows
#: are fixed by construction, so "no rows" would be a bug rather than a state to describe.
#:
#: A sentinel rather than `None`, because the two answers have to be distinguishable: `None`
#: means *this screen has not been asked yet*, which is what `test_empty_states.py` fails a
#: newly added screen on. Left as a plain `None` default, a sixteenth screen would inherit
#: silence and the exhaustiveness check would pass over it.
NEVER_EMPTY = "\x00never-empty"


def held_option_id(pane: OptionList) -> str | None:
    """Which row the cursor is on right now, or None if it is on nothing.

    Three call sites had this same five-line expression -- `SessionsScreen._draw_listing`,
    `DashboardScreen._reload_sessions_pane` and `FeedRegion._draw_feed` -- each with its own
    bounds check spelled a slightly different way, and each docstring citing the others'
    reasoning by name rather than sharing their code. That is the shape BL-031 records and
    that `test_no_adapter_redefines_the_row_or_the_area_predicate` sweeps for: copies that
    agree on the day they are written with nothing keeping them agreeing.

    The bounds check is the part worth having once. `highlighted` can outlive the row it
    names -- a background reload shortens the list under a cursor that was near the end --
    so reading `get_option_at_index` without it raises on exactly the tick the restore exists
    to survive.
    """
    held = pane.highlighted
    if held is None or held >= pane.option_count:
        return None
    return pane.get_option_at_index(held).id


def restore_highlight_by_id(pane: OptionList, held_id: str | None, keys: Sequence[str]) -> None:
    """Put the cursor back on the row it was on, or on the first row.

    By key and never by index: these lists are newest-first and grow at the head, so the
    index the owner was on names a different thing after any reload. Row 0 is the fallback
    because the cursor must always rest somewhere non-mutating (DEC-007), never nowhere.
    """
    if not keys:
        return
    target = keys.index(held_id) if held_id in keys else 0
    # Never onto a disabled row. Textual's own guards do not cover this path: `validate_
    # highlighted` only clamps to bounds, and `watch_highlighted` merely skips the scroll and
    # the `OptionHighlighted` post for a disabled index -- neither refuses to *set* it. So a
    # direct assignment can park the cursor on a row that cannot be selected, cannot be
    # scrolled to, and fires no highlight event: stranded, and invisible to a snapshot.
    #
    # Not currently reachable -- an expanded feed's continuation ids cannot collide with a row
    # key -- but that safety is an emergent property of three modules' constraints agreeing
    # (session ids forbid colons, the kind vocabulary is a closed enum, timestamps contain no
    # "detail"), asserted nowhere. This function's own contract is that the cursor rests
    # somewhere non-mutating, so it is enforced here rather than left to those three.
    if _is_disabled(pane, target):
        target = next(
            (index for index in range(len(keys)) if not _is_disabled(pane, index)), target
        )
    pane.highlighted = target


def _is_disabled(pane: OptionList, index: int) -> bool:
    return index < pane.option_count and pane.get_option_at_index(index).disabled


class ChoiceScreen(Screen[None]):
    """A status line, an optional filter, a list of choices, and an output pane.

    Every position in the surface renders this same body, which is what lets the committed
    snapshot baselines survive the extraction: the widget tree a screen composes here is the
    tree the app used to compose once and repaint in place.

    Deliberately **not** re-exported from `screens/__init__`. That namespace is swept for
    `Screen` subclasses missing from `ALL_SCREENS` — by the sub-plan's Stage 2 gate, and on
    every run by `test_screen_back_paths.py`'s exhaustiveness check — and this base is not a
    position the owner can navigate to, so exporting it would fail a check that is asking a
    fair question.
    """

    #: Shown in the status line until the screen replaces it. One line, checked at class
    #: creation by `__init_subclass__` for the reason given there.
    status = ""
    #: When set, the filter input is visible and carries this placeholder.
    filter_placeholder: str | None = None
    #: The name this position is committed under in the snapshot baselines. Declared on the
    #: screen rather than mapped in the test so that adding a screen and forgetting its
    #: baseline is a missing name here, not a silently uncovered position there.
    position = ""
    #: How a failed read tells the owner where to go from here. Declared per screen because
    #: it names a *key* and a *position*, and both depend on where this screen is sitting: a
    #: pushed flow can escape back, while a screen that is its own process's resting position
    #: cannot — `go_back` refuses on the last screen, so the sentence would name an inert key
    #: and a place that does not exist in that process. Overridden by the console's panes.
    read_failure_route = "Press escape to return to the project list."
    #: Whether this position has anything to re-read, i.e. whether Refresh means something
    #: here. Declared beside `refresh_contents` rather than inferred from whether the hook is
    #: overridden, because Task 1.2 has to ask this question from `check_action` — before any
    #: refresh runs, to decide whether the footer advertises the key at all — and "did this
    #: class replace a method" is not a question a binding check should be asking.
    can_refresh = False
    #: Whether text typed into this screen's entry is a *value being gathered* rather than a
    #: filter narrowing a list. Set on the screens that commit it with `submit`, and pinned
    #: against that by a test — the same two-declarations-of-one-fact hazard `can_refresh` has.
    #:
    #: The distinction is what the owner loses. A label or a project name is the payload of the
    #: step they are on and cannot be recovered; a filter is one keystroke to retype and sits
    #: on the resting position, where leaving for another flow is the ordinary thing to do.
    entry_is_a_commitment = False
    #: The one line this position shows when it has no rows — "No sessions are running", not a
    #: blank rectangle. `NEVER_EMPTY` for a position whose rows are fixed by construction.
    #:
    #: `None` is not a valid answer, it is the *absence* of one, and `test_empty_states.py`
    #: fails any screen still carrying it. That is what makes the check exhaustive rather than
    #: a list of the four screens someone thought of: adding a screen forces the question.
    empty_state: str | None = None
    #: What this position is called in the header's breadcrumb. A screen whose name depends on
    #: what it was opened with overrides this as a property — the session detail is called
    #: after its session, not after its class.
    #:
    #: Empty means "leave me out of the trail", which is the honest answer for a position that
    #: adds nothing to it. Declared per screen rather than derived from the class name so that
    #: renaming a class does not silently rewrite what the owner reads.
    crumb = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Keep `status` a single line at the point a screen declares one.

        The class-level `status` is the same sink `set_status` writes to, and it reaches it
        without passing through that method's guard: `compose` constructs the widget with it
        directly — `Static(self.status, id="status", …)` — before `on_mount` and its
        `set_status` call ever run. So the guard has to exist twice or it does not exist:
        once at the call, once here. This one fires at import, which is where a two-line
        default belongs to be caught.

        (An earlier version of this paragraph named `on_mount` as the bypass. It is not one —
        it goes through `set_status` like any other caller — and the correction is worth
        keeping because a reader sent to the wrong method would find the guard working there
        and conclude this check was redundant.)
        """
        super().__init_subclass__(**kwargs)
        # `isinstance` first, and not defensively. `crumb` two attributes above documents
        # overriding as a property as the supported idiom, and six screens do it — so the first
        # screen wanting a dynamic `status` used to get `TypeError: argument of type 'property'
        # is not a container` at import, naming neither the attribute nor the rule. Refused
        # explicitly instead: the value is read by `compose` before any instance exists, so a
        # property genuinely cannot work here, and saying so is the whole job of this check.
        if not isinstance(cls.status, str):
            raise TypeError(
                f"{cls.__name__}.status must be a plain string — `compose` reads it before "
                "there is an instance to compute one from. Set it in `populate` instead."
            )
        if "\n" in cls.status:
            raise ValueError(
                f"{cls.__name__}.status must be one line; the status region is one line high"
            )

    def __init__(self) -> None:
        super().__init__()
        # Bumped by every fill; a deferred cursor placement carries the value it was
        # scheduled with and stands down if a later fill has superseded it.
        self._resting_generation = 0
        #: The last validation failure announced from this screen's entry, so the same one is
        #: not repeated on every subsequent keystroke that keeps breaking the same rule.
        self._last_rejection: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            # `markup=False` for the reason given at `#choices` below: `#status` is handed the
            # conversation description (`_resolve_resume_conversation`) and
            # `record.display.rendered`, which interpolates the owner's custom label, and an
            # unbalanced bracket in either raised `MarkupError` — text this app echoes from
            # another program could take down the screen showing it.
            yield Static(self.status, id="status", markup=False)
            yield Input(placeholder=self.filter_placeholder or "", id="filter")
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
                # A read-only `TextArea` rather than the `Static` this used to be. The pane
                # carries up to `_INSPECT_MAX_LINES` (2000) lines of captured agent output,
                # and a `Static` offers no way to select inside it, search it, or jump to its
                # end — the three things an owner reading 2000 lines actually does.
                #
                # It is **not** given `markup=False`, because there is no such flag and none
                # is needed: `TextArea` never parses console markup. It renders each line
                # through `TextArea.get_line`, which builds `rich.text.Text(line_string)` —
                # the plain constructor, not `Text.from_markup` — so the `MarkupError` that
                # the old `Static` needed the flag to avoid cannot arise here at all. That is
                # pinned by `test_row_markup.py`, not assumed: the untrusted string this sink
                # receives is the session's raw pane output, which `sanitize_terminal_text`
                # filters for control sequences and NUL but never for brackets.
                #
                # `highlight_cursor_line=False` because the highlight is drawn whenever the
                # widget has a usable cursor, focused or not (`_has_cursor` is true for a
                # read-only area that still shows its cursor), which would put a `$boost`
                # band across the first line of every capture the owner has not touched.
                yield TextArea(
                    "", id="output", read_only=True, soft_wrap=True, highlight_cursor_line=False
                )
        yield Footer()

    async def on_mount(self) -> None:
        """Set the chrome this screen asked for, then let it fill itself.

        A template method rather than an overridable `on_mount`, so a screen cannot forget
        the chrome by defining its own handler.

        The output pane used to be hidden here, imperatively, on every screen. It is now
        hidden by the app's CSS and revealed by a class — so the default is declared once in
        the stylesheet instead of being re-established by whichever method happened to run,
        and a screen that never calls `show_output` needs no line of code to stay on the list.
        """
        self.show_breadcrumb()
        entry = self.query_one("#filter", Input)
        entry.display = self.filter_placeholder is not None
        if self.status:
            self.set_status(self.status)
        await self.populate()

    async def populate(self) -> None:
        """Render this screen's rows. Overridden by every concrete screen."""

    async def on_reveal(self) -> None:
        """Re-render whatever this screen shows, because it just became active again.

        Called by `RemoteAgentsTui.go_back` after the screen above this one is popped, so a
        position whose content can go stale while the owner is elsewhere gets a fresh read on
        the way back — which is what the hand-rolled chain did by re-running the whole
        `_show_*` on every back path.

        An explicit hook rather than Textual's `ScreenResume`, because `go_back` can *await*
        this one. A resume handler runs on the pump after the pop returns, which would put
        the re-read outside the busy guard a stop is still holding — the window in which a
        keypress acts on a screen the owner is no longer looking at. Screens with nothing to
        re-read leave it as this no-op.
        """

    async def refresh_contents(self) -> None:
        """Re-read whatever this screen shows, because the owner asked for it directly.

        What Ctrl+R does. A per-screen capability rather than an app-level action, because
        the app-level one re-read the *project catalogue* and then unwound to the project list
        unconditionally — so the key the footer calls Refresh, pressed on the sessions list,
        abandoned the one position in this surface whose answer goes stale on its own: a
        second process writes the same store, and that list is where the owner would notice.

        Separate from `on_reveal` despite both meaning "re-read", and the catalogue is the
        distinction. `on_reveal` fires on every back path, where re-reading the development
        root would put a disk scan on the way out of every flow; this fires only when the
        owner asked for it, which is when that cost is the whole point. A screen wanting the
        same work for both says so by calling one from the other.

        A screen with nothing to re-read leaves this as the no-op it is, and leaves
        `can_refresh` false alongside it.
        """

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Whether an app-level binding applies here — `False` hides it from the footer.

        **Every rule below mirrors an early return that already exists in the action it
        governs.** That is the whole design, and it is what keeps this from becoming a second
        opinion that can drift from the first: the footer stops advertising keys that already
        did nothing, rather than a separate list of what is legal where. If an action grows a
        new early return, the honest change is to mirror it here — and if a rule here has no
        counterpart in the action, one of the two is lying.

        `False` rather than `None` throughout, and the two are easy to swap: `DOMNode.check_action`
        documents `False` as **disabled and hidden** and `None` as **disabled but still drawn,
        greyed** — and `Screen.active_bindings` implements exactly that, dropping the entry only
        on `is False`. This sub-plan's own research summary states the opposite, which is how the
        first version of this method hid nothing at all while every test that asked the framework
        said so. A key the owner can see and cannot use is the same complaint as a key that does
        nothing, one step quieter, so hiding is what these want.

        Quit and the two flow jumps that always work are deliberately absent from the checks:
        an unconditional `True` for them would read as a rule and is not one.

        **`_busy` is the one early return deliberately not mirrored, and the claim above is
        false without saying so.** It gates all five of these actions, so while a command is in
        flight every key here is advertised and does nothing — which is the same complaint,
        during a window measured in the length of a store read. It is left alone because the
        honest fix is not a hidden key: a footer whose entries vanish and reappear as the
        surface works would be worse than one that is briefly optimistic.

        **The one rule here that mirrors no early return is the work-in-flight one, and it is
        deliberate.** The three flow jumps unwind the stack, so pressing one while a name is
        half-typed — or while the add-project review holds one already committed — discards it
        with no warning and no way back. The action has no early return for that
        because the action never knew. This is the rule that had to be invented rather than
        mirrored, and it is the only one answering `None`: the key stays drawn and greyed rather
        than vanishing, because a footer entry that disappears as the owner types would be a
        second surprise on top of the first.

        **Quit is deliberately not in that set, and saying so is the point.** `ctrl+q` discards
        in-flight work exactly as the three jumps do, and a stage review reproduced it on the launch
        wizard's label entry, since removed: type a name, press it, the app is gone and the name
        with it. The same is reproducible today on the project-name entry. It is left enabled
        because the two keys mean different things to the person pressing them. The jumps mean
        "go somewhere else in this app", and losing the work is a side effect nobody asked for;
        quit means
        "leave", and an app that refuses to close until an entry is cleared is a worse answer
        than the one it replaces.

        **It now warns instead**, which is the shape that argument always pointed at:
        `RemoteAgentsTui.action_quit` announces what is about to be lost and arms, and the
        second press leaves regardless. So quit stays out of this set on purpose — greying it
        would be the refusal — and the protection lives on the action rather than in the
        footer. `work_at_risk` is the companion to `work_in_flight` that lets the warning name
        what it is warning about.
        """
        if action == "back":
            # `go_back` refuses to pop the last screen, so at the resting position escape is
            # inert by construction — see `RemoteAgentsTui.go_back`.
            return False if len(self.app.screen_stack) <= 1 else True
        if action == "refresh":
            # Mirrors `action_refresh`, which now delegates to `refresh_contents`.
            return True if self.can_refresh else False
        if action == "resume" and self.services.backend.conversations is None:
            # Mirrors `action_resume`'s early return on the same read: a host that wired
            # no conversation service has no resume flow to open, and the
            # binding has been advertised on those hosts all along. Checked before the
            # in-flight rule below so a host without the capability hides the key outright
            # rather than greying it, which would imply it were available later.
            return False
        if action in _FLOW_JUMPS and self.work_in_flight:
            return None
        return True

    @property
    def work_in_flight(self) -> bool:
        """Whether leaving this position would discard something the owner built.

        Not "is there text in the box" — that was the first version of this, and a stage review
        found what it missed. A project name is protected while it is being typed and then
        *committed*, at which point the box is empty and the same three keys throw away the
        gathered result from the review step one screen later. The thing worth protecting was
        never the widget's contents; it is work the owner cannot get back by pressing escape.

        **The launch wizard used to be the example, and it has stopped being one entirely.**
        Its label step illustrated this better than anything that replaced it, and when that
        step went, the review left behind held two list selections re-pickable in two
        keystrokes — so it protected nothing and stopped overriding this. That review has since
        been removed too, and what inherited the launch, the agent list, holds no gathered work
        at all: the project sits one screen below it, still drawn. The whole flow is now on the
        default's side of this question, and `ProjectReviewScreen` is the one screen left
        overriding it — a typed project name that `NameScreen.populate` clears is still work
        escape cannot give back.

        So a screen that holds gathered state says so by overriding this, and the default
        answers for the ordinary case: an entry that is shown, non-empty, and a commitment. The
        entry has to be *shown* as well as non-empty because `hide_entry` leaves the widget in
        the tree with a stale value, so reading the value alone reports work in flight on
        positions that have no entry at all.
        """
        if not self.entry_is_a_commitment:
            return False
        # `query_one` raises `NoMatches` before the screen has composed, and this runs from
        # `check_action` — the same path `App.check_action` already guards its own dereference
        # on. No driven sequence reaches it (a gate evaluator tried, across a full keyed walk
        # and twenty unawaited-push bursts), because every real consumer runs from the pump
        # after mount. Guarded anyway: an exception out of a footer redraw is the class that
        # has already cost this app once, and "unreachable today" is what that was too.
        return self._live_entry() is not None

    def _live_entry(self) -> Input | None:
        """The entry, but only when it is shown and holds something — else `None`.

        The one guarded dereference `work_in_flight` and `work_at_risk` both read, so the two
        cannot answer from different premises. They were written with a near-verbatim copy of
        this each, differing only in what they returned once the entry was found live, and a
        Tier-1 review pointed out that widening what counts as "shown" would then have to be
        done twice to stay correct.

        The entry has to be *shown* as well as non-empty because `hide_entry` leaves the
        widget in the tree with a stale value, so reading the value alone reports work in
        flight on positions that have no entry at all.

        `query_one` raises `NoMatches` before the screen has composed, and this runs from
        `check_action` and from a global binding — the same path `App.check_action` already
        guards its own dereference on. No driven sequence reaches it (a gate evaluator tried,
        across a full keyed walk and twenty unawaited-push bursts), because every real consumer
        runs from the pump after mount. Guarded anyway: an exception out of a footer redraw is
        the class that has already cost this app once, and "unreachable today" is what that
        was too.
        """
        entry = self.query("#filter").first(Input) if self.query("#filter") else None
        return entry if entry is not None and entry.display and entry.value else None

    @property
    def work_at_risk(self) -> str:
        """What leaving would discard, named so a warning can quote it back.

        `work_in_flight` answers *whether* there is something to lose; this answers *what*, and
        the two are separate because a warning that cannot name what is at risk is not much of
        a warning — "you have unsaved work" is the message every owner has learned to dismiss.

        The default is the entry's own text, which is the thing the owner just typed. A screen
        that overrides `work_in_flight` because it holds gathered state rather than a typed
        entry should override this too; the empty string is the honest answer for a screen
        that cannot name its work, and the quit warning falls back to a general sentence
        rather than quoting nothing.

        Reads `_live_entry` rather than repeating its guard, so this and `work_in_flight`
        cannot disagree about whether there is an entry to speak of.
        """
        entry = self._live_entry()
        return entry.value if entry is not None else ""

    # Rendering -----------------------------------------------------------------

    @property
    def tui(self) -> RemoteAgentsTui:
        """The app, typed. Screens reach the services and shared selection through it."""
        return cast("RemoteAgentsTui", self.app)

    @property
    def services(self) -> TuiContext:
        return self.tui.services

    @property
    def showing(self) -> bool:
        """Whether this screen is still the one the owner is looking at.

        Every render below is guarded by this, and the guard is the *class* fix for a defect
        that has now been found twice in this surface: a coroutine awaits a store read, the
        owner presses Escape, the screen is popped, and the coroutine resumes and writes to
        widgets that are gone. Textual raises `NoMatches` there, and an exception out of a
        message handler takes the whole app down — from the very code paths that exist to
        report trouble *without* losing the app.

        `is_mounted` cannot answer this and must not be used for it: it stays `True` after a
        pop while the screen's widgets are already unmounted, so a `query_one` still raises.
        Identity against `app.screen` flips synchronously with the pop, which is what makes it
        usable from inside a coroutine that awaited across one.

        **`App.screen` itself raises on an empty stack**, and this now answers `False` there
        rather than propagating — the same guard `RemoteAgentsTui.check_action` carries, for
        the same reason, and it belongs here rather than at each caller. `awaiting`'s `finally`
        reaches this during teardown, and at `ProjectReviewScreen.choose` that `finally` is the
        outermost context with no `except` around it, so the exception would leave a message
        handler. "Nothing on the stack" and "this screen is not what the owner is looking at"
        are the same answer anyway. Found by the Stage 2 gate's second review pass.

        Guarding here rather than at each call site is deliberate. A sweep of this package
        found eleven methods that await and then render or push; adding a caller-side guard to
        each is precisely the arrangement that let two of them be missed when their four
        siblings got one.
        """
        try:
            return self.app.screen is self
        except ScreenStackError:
            return False

    @contextlib.asynccontextmanager
    async def holding_the_guard(self) -> AsyncIterator[None]:
        """Hold the surface's busy guard for the duration of a store read.

        Extracted because the same six-line `set_busy(True)` / `try` / `finally` body was
        copy-pasted across six methods and *omitted* from two more, which is how the
        Escape-mid-read defect survived its first repair. One helper is one thing to reach
        for; eight near-identical bodies are eight chances to forget — and all eight go
        through this now, not just the two the review happened to name.

        This prevents the situation — `action_back` refuses to pop while it is held — where
        `showing` merely makes the aftermath harmless. Both are kept: the guard is the narrow
        fix for the paths that can afford to block, `showing` is the one that covers every
        path including the ones that cannot.
        """
        self.tui.set_busy(True)
        try:
            yield
        finally:
            self.tui.set_busy(False)

    @contextlib.asynccontextmanager
    async def awaiting(self, doing: str) -> AsyncIterator[None]:
        """Cover the rows and say what is happening, while a command is in flight.

        Deliberately **not** the same window as `holding_the_guard`, and the two are easy to
        conflate because four of the five flows hold both. The guard means "no other action
        may start", which includes the whole time a confirmation modal is open — the owner may
        deliberate for as long as they like — and a surface that spins while it waits for a
        person to answer a question is lying about what it is doing. This covers only the part
        where something outside this process has been asked and has not replied.

        `#choices` rather than the screen, because the rows are the thing that must not be
        acted on and the status line is the thing that has to keep saying something true.
        Textual's `set_loading` *covers* the widget rather than replacing it, so a re-render
        arriving while this is held still writes to the real `OptionList` and is revealed
        intact — and `Widget._check_disabled` already reads `self.loading`, so the covered rows
        stop accepting input from Textual's own dispatch and not only from the busy guard.

        **`doing` is required rather than optional, and that is the whole of the second
        half.** The first version covered the rows and left the status line alone, on the
        argument that it stayed readable underneath. A review pointed out what it was left
        saying: the launch commit position's own line — "Label: none. Launch, or go back." at
        the time, and a sentence about the terminal handover now, on the agent list that has
        since inherited both the act and the sentence — is an instruction about a row that is at
        that moment covered and refusing input. A
        line that was true a moment ago and is false now is worse than the spinner alone,
        because the owner has no reason to doubt it. So the flow names what it is doing, and
        the previous line is put back on the way out, before the caller writes its own result
        over it. Every one of the five happens to re-render afterwards, so the restore is
        belt and braces — but relying on all five continuing to is how the next one leaves
        "Stopping…" on screen after the stop has landed.

        Both edges are guarded by `showing`, not just the first. A screen popped mid-command —
        which is the whole reason `showing` exists in this file — leaves an unmounted widget
        behind, and `query_one` raises `NoMatches` on it. **No driven sequence reaches the
        asymmetric case**: every route that changes `app.screen` is refused while the busy
        guard is held, and all five flows hold it across the whole of this window. Guarded
        anyway, on the same grounds as `work_in_flight`'s own guard — an exception out of a
        `finally` is the class that has already cost this app once, and clearing a position
        that was never covered is a no-op (`_uncover` returns early on `_cover_widget is
        None`), so the guard costs nothing to keep honest.
        """
        previous = self._status_text()
        self._set_working(True, doing)
        try:
            yield
        finally:
            # Restored **only if nothing wrote a new line meanwhile**. Without that condition
            # this is a clobber waiting for its first caller: anything that redraws from inside
            # the cover — a refusal calling `refuse`, a screen re-rendering from a fresh read —
            # sets the status the owner should be left with, and an unconditional restore would
            # replace it with the instruction from before the command ran. Comparing against
            # `doing` rather than tracking a flag keeps that true for a redraw this class never
            # sees, which is the only kind there will be.
            self._set_working(False, previous if self._status_text() == doing else None)

    def _set_working(self, working: bool, text: str | None) -> None:
        if not self.showing:
            return
        self.query_one("#choices", OptionList).loading = working
        if text is not None:
            self.set_status(text)

    def _status_text(self) -> str:
        """What the status line currently reads, so `awaiting` can put it back.

        `str(...)` rather than `.plain`. An earlier version of this paragraph explained the
        cast by claiming `Static.content` is a `str` under `markup=False` and a `Content`
        otherwise; that is not what the widget does — `content` returns whatever was last handed
        to the constructor or `update()`, and knows nothing about markup. The cast is right for
        a duller reason: `set_status` is the only writer and only ever passes `str`, so this is
        an identity today and insurance against the day something passes a `Content`. Corrected
        rather than deleted, because a reader who believed the old reason would conclude the
        cast becomes removable the moment `#status` gains markup, which is backwards.
        """
        return str(self.query_one("#status", Static).content) if self.showing else ""

    async def advance_to(self, screen: Screen[None]) -> None:
        """Push `screen`, unless this one has been left while a read was in flight.

        The other half of `showing`. A push from a screen that is no longer on top lands on
        whatever replaced it — which is how an output pane ends up sitting above a position
        that is not showing the session whose output it captured.

        The example used to be the force confirmation, which was pushed through here. It is a
        `ModalScreen` awaited through `ask_to_confirm` now and does not come this way at all,
        so the hazard is the same and the illustration had to change with the call sites: the
        inspect screen and the three resume pushes are what still travel through this.

        **The navigation guard narrows this window; it does not close it, and the callers'
        comments should not be read as saying otherwise.** Holding the guard across a fetch
        stops *this app's* bindings from leaving mid-read, because they consult `busy` — but
        Textual installs the command palette's `ctrl+p` with `priority=True`, so it is checked
        on the App's own pump, which a suspended screen handler does not hold, and
        `check_action` here lets it through. Opening the palette during a guarded fetch
        therefore still reaches this early return. Measured against Textual 8.2.8; the palette
        binding is added in `App.__init__` when `ENABLE_COMMAND_PALETTE` is set.

        That return is also the one exit from a resume `choose` that says nothing at all —
        every other branch announces — so the owner sees their selection do nothing. It is not
        a regression and nothing unsafe happens (the palette's own entries all gate on `busy`),
        but giving it a voice is a UX call with a real argument on both sides: a screen the
        owner has already left is a questionable place to put a message. Recorded rather than
        decided here.
        """
        if not self.showing:
            return
        await self.app.push_screen(screen)

    # The three sinks -------------------------------------------------------------
    #
    # One region per kind of thing the surface has to say, because they were sharing one and
    # the sharing is the defect. `#status` was `height: auto`, so an instruction one line long
    # and a failure four lines long were the same widget at two different sizes, and the rows
    # below it moved every time one replaced the other. Which one an owner is reading was also
    # left to them to work out: "Choose a project", "Project not created: …" and
    # "3 managed session(s)" are an instruction, a failure and a result, and they arrived in
    # the same place in the same voice.
    #
    #   breadcrumb  — where the owner is. Header sub-title, set on mount and whenever the
    #                 thing this position is *about* is read again.
    #   status      — what to do here. Exactly one line, so the body below it never moves,
    #                 and it belongs to the position rather than to any one action.
    #   announce    — what an action just did or failed to do. A toast, because it is about
    #                 the action and the position outlives it.

    @property
    def breadcrumb(self) -> str:
        """The trail of positions the owner walked to get here.

        Built from the stack rather than declared per screen, which is the only form that
        cannot go stale: a screen names itself with `crumb` and knows nothing about what it
        was pushed from, so the same detail reached from the sessions list and from a launch
        reads correctly both times without either flow maintaining a path.

        `self` is appended when it is not on the stack yet, because this is called from
        `on_mount` and a pushed screen is mounted before `App.push_screen` appends it —
        without that the screen the owner is being shown is the one missing from its own
        trail. Identity rather than `in`: `DOMNode` inherits `object.__eq__`, so this is the
        same comparison either way today, and spelling it out keeps it that way if a screen
        ever gains a value-based one.
        """
        trail = [screen for screen in self.app.screen_stack if isinstance(screen, ChoiceScreen)]
        if not any(screen is self for screen in trail):
            trail.append(self)
        return " › ".join(screen.crumb for screen in trail if screen.crumb)

    def show_breadcrumb(self) -> None:
        """Put the trail in the header. Called again by a screen whose own crumb has moved.

        **The one render in this class deliberately not guarded by `showing`**, which is worth
        saying because every other one is and the omission would otherwise read as an
        oversight. Two reasons, and both are needed:

        * It runs from `on_mount`, where `app.screen` is not yet this screen — the same
          ordering `breadcrumb` compensates for — so a `showing` guard would skip the
          breadcrumb on the very call that establishes it.
        * The failure it would be guarding against does not exist here. `sub_title` is a
          reactive on the screen itself and each screen composes its own `Header`, so writing
          to a popped screen's sub-title repaints a header nobody is looking at. That is
          unlike `query_one("#status")`, which raises `NoMatches` once the widgets are gone —
          which is the thing `showing` exists for.
        """
        self.sub_title = self.breadcrumb

    def set_status(self, text: str, *, severity: SeverityLevel = "information") -> None:
        """Say, in one line, what to do here or what just happened.

        One line is a *contract*, not a suggestion the CSS enforces: `#status` is one line
        high, so a second line is not clipped visibly, it is invisible. The first line is
        rendered and the whole value is logged, which turns a silent loss into one that can be
        found — and the log is not the fallback nobody reads, because a static check over this
        package's own call sites (`test_status_region.py`) fails on a literal that contains a
        newline. The runtime guard is what catches the values a static check cannot see: an
        exception's `str()` carries newlines from the library that raised it, and every
        failure path in this surface interpolates one.

        `severity` colours the region from the design system — `$error`, `$warning` — rather
        than from a literal, so it resolves per theme instead of assuming a dark one.

        **Colour is the second signal here and never the only one, and that bounds which
        callers may pass a severity at all.** A terminal under `NO_COLOR`, a monochrome
        profile, and a colour-blind reader all get the same characters and different
        palettes, so a status whose severity lived in its colour would say nothing to any of
        them.

        The rule that follows is sharper than "add colour to the failure paths", and it was
        learned by breaking it: this method was first wired into every path that *followed* a
        failure, which painted `Press escape to return to the project list.` red. That
        sentence reports nothing. Driven under `NO_COLOR` and under the ANSI theme, the whole
        surface then said only where to go next, in three renderings that looked identical —
        severity by colour alone, which is precisely what this region must not do.

        So: pass a severity only when **this string** names the condition. The region split
        sub-plan 3 built means most failure paths deliberately put the *why* in a toast and
        the *what next* here, and those keep the neutral colour they have always had.
        """
        if not self.showing:
            return
        if "\n" in text:
            _LOG.warning("a multi-line status was truncated to its first line: %r", text)
            text = text.split("\n", 1)[0]
        region = self.query_one("#status", Static)
        region.set_class(severity == "error", "-error")
        region.set_class(severity == "warning", "-warning")
        region.update(text)

    async def refuse(
        self, message: str | None = None, *, severity: SeverityLevel = "warning"
    ) -> None:
        """Say an action will not happen, then redraw from the read that established it.

        The redraw is the part that is easy to leave out and the part that matters. Every
        caller reaches here having just re-read the record and found it disagrees with what
        the rows were built from — the session is gone, or has moved to a state the policy no
        longer offers this action for. Reporting and returning leaves those rows on screen,
        still offering a stop for a session that ended while the owner was reading.

        `message` is optional because the two refusals differ in exactly one way. A session
        that has *vanished* needs nothing said here: `render_detail` writes "That session is no
        longer available." as part of the redraw, and announcing it as well would show the same
        sentence twice for one event, in two places, which `test_confirm_modals` already pins
        against. A session that has *moved on* needs the toast, because the redraw will show
        its new state without ever saying that something was attempted and refused.

        `warning` by default rather than `error`: the session moving under a rendered row is
        the race DEC-007's re-read exists to catch, not a fault. Extracted after a gate review
        found this written out by hand at eight sites across two files — six of them
        character-identical, which is how the two that were subtly different went unnoticed.
        """
        if message is not None:
            self.announce(message, severity=severity)
        await self.on_reveal()

    def announce(self, message: str, *, severity: SeverityLevel = "error") -> None:
        """Say what an action did, over the position rather than instead of it.

        A toast, for the reason the region split exists: a failed stop is about the stop, and
        the detail underneath it is still the answer to "what am I looking at". Rendering the
        failure into the status line meant the position lost its own description for as long
        as the message stayed — which was until something else overwrote it, since nothing
        ever cleared it.

        Named for what it does rather than for the common case: `error` is the default because
        most of what an action has to report here is a failure, but a creation that succeeded
        and a refusal that is nobody's fault both come through here too, and a method called
        `report_failure` carrying an `information` severity would be a lie at its own call
        site.

        Delegated to the app rather than calling `Widget.notify` directly so `markup=False` is
        decided once. `Toast` renders console markup by default, and every message that
        reaches here interpolates something this app does not author: an exception's text, an
        agent's own output, the owner's label. An unbalanced `[` in any of them raises
        `MarkupError` out of the notification path — which is the same defect, in the same
        surface, that `markup=False` on `#status` was added for — and on `#output` too, until
        that pane became a `TextArea` that cannot parse markup at all. Escaping at each call
        site is what let those go unnoticed for three sources at once.
        """
        self.tui.announce(message, severity=severity)

    @staticmethod
    def _unique_by_key(entries: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        """Drop a repeated key, keeping the first, so a duplicate cannot take the screen down.

        `OptionList.add_options` raises `DuplicateID` when two options in one batch share an
        `id` (`_option_list.py:379-382`), and it raises *within* a single call rather than only
        against options already added. The widget this replaced tolerated a repeated key
        silently — it was a plain attribute with no uniqueness rule — so the migration turned
        "renders an ambiguous list" into "raises uncaught inside the fill".

        That matters at exactly one call site today and it is the one fed by data this app does
        not own: the resume conversation list keys its rows on `ConversationReference`s built by
        the agent adapters from on-disk provider state. Its `try/except` covers the catalogue
        await, not the fill below it, so a provider reporting one conversation twice on a
        page would crash the screen. Every other caller keys on ids this app controls.

        Deduplicating here rather than at that one site because this is the single choke
        point every row set passes through, so a future screen fed by another external source
        cannot reintroduce the crash by forgetting a guard. Keeping the first occurrence
        preserves the old behaviour's outcome: under the previous widget both rows rendered and
        selecting either dispatched the same key, so one row and the same key is the closer
        match to what the owner used to get — and strictly better than an exception.

        The drop is logged rather than silent: a page that lost a row is a provider bug worth
        being able to find, and a silent dedup would hide it exactly as the crash would.
        """
        seen: set[str] = set()
        unique: list[tuple[str, str]] = []
        for key, text in entries:
            if key in seen:
                _LOG.warning("dropped a repeated row key from the surface: %r", key)
                continue
            seen.add(key)
            unique.append((key, text))
        return tuple(unique)

    def show_choices(
        self,
        entries: tuple[tuple[str, str], ...],
        *,
        focus: bool = True,
        highlight: int | None = 0,
        trailing: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """Render the choices, and take the keyboard only when the list is the next decision.

        Refilling while the owner is typing a filter must leave the keyboard where it is, or
        every character after the first lands on the list instead of the query.

        `entries` are the position's *data* rows and `trailing` are its fixed navigation rows
        — Back, Previous page, Next page. The split exists for one reason: emptiness is a fact
        about the data, and a list holding nothing but a Back button is empty in every sense
        the owner cares about while being a non-empty tuple. Passing the Back row through
        `entries` is how a screen silently opts out of its own empty state.

        `highlight=None` means **rest the cursor on nothing** — draw no selection and leave the
        next arrow press to choose. It composes with `focus` rather than overriding it: the
        keyboard still goes where `focus` says, and only the cursor is withheld.

        **Three callers ask for it, and they share a predicate rather than a screen.** The rule
        is *a repeated keypress must not commit what the owner did not choose*, so a position
        rests on nothing exactly where the cursor would otherwise sit on something the next
        enter would act on: `SessionsScreen._draw_listing` when the row the cursor held has left
        the list, `SessionsScreen.redraw_after_failure` when a stop raised and the row it failed
        on is still there, and `ProfilesScreen.render_profiles(resting=False)` after a launch
        that failed, where the agent row the owner pressed is still under the cursor and
        `_busy` has already been cleared. An earlier version of this paragraph said "exactly one
        caller … and the sessions positions are the only ones where it would be correct"; the
        second and third callers arrived in the same plan that wrote it.

        Every other position keeps `0`, because a list advertising "enter opens" with nothing
        highlighted makes its own keys silent no-ops for no benefit.
        """
        if not self.showing:
            return
        if not entries and self.empty_state not in (None, NEVER_EMPTY):
            entries = ((_EMPTY, str(self.empty_state)),)
            # Rest on the first row that does something. The substituted row is disabled, so a
            # cursor left on it would answer enter with nothing at all — and where there *is*
            # a Back row, that is the one action still available here.
            #
            # Overrides a `None` too, and must: "rest on nothing" is a statement about a list
            # whose rows the owner was choosing between, and this branch has replaced them all
            # with a placeholder. The one caller that passes `None` cannot reach here anyway —
            # `_draw_listing` returns on an empty listing before its keep-cursor path — so this
            # is the assignment agreeing with that rather than a second policy.
            highlight = 1 if trailing else 0
        entries = entries + trailing
        # Restoring here rather than in each exit route: the inspect screen swaps the list
        # for a scrollable output pane, and every other screen renders through this, so
        # this is the one place that cannot be forgotten by a new navigation path.
        self.hide_output()
        self._resting_generation += 1
        choices = self.query_one("#choices", OptionList)
        choices.clear_options()
        entries = self._unique_by_key(entries)
        # The key is the `Option`'s own `id`, which is what the selection message carries
        # back as `option_id`. It replaces the attribute this used to bolt onto each mounted
        # row: `OptionList` mounts no widget per row, so there is nothing to attach to, and
        # row identity is first-class rather than monkey-patched.
        choices.add_options(Option(text, id=key, disabled=key == _EMPTY) for key, text in entries)
        if entries and highlight is None:
            # Resting on nothing, and **cleared whether or not this fill takes the keyboard**.
            # The `focus` flag says where the *keyboard* goes; the cursor is a different
            # question, and on a list carrying unconfirmed stop keys it is the safety-relevant
            # one. `clear_options` does not reset `highlighted`, and `validate_highlighted`
            # clamps rather than rejects — so a fill that skipped this because `focus` was
            # False would leave the old index pointing at whatever row now sits there, which is
            # precisely the silent move this branch exists to prevent. Guarding it on `focus`
            # would make the mitigation depend on where the keyboard happened to be.
            #
            # The focus call keeps its guard, because that half really is about the keyboard:
            # taking it from a filter the owner is typing into is the defect `focus` exists for.
            # Where this fill does take it, focus is what makes the arrow press that brings the
            # cursor back possible at all.
            #
            # No `_rest_cursor` callback is scheduled: it exists to re-run
            # `scroll_to_highlight` once the widget has a laid-out region, and there is nothing
            # to scroll to. The generation counter was already bumped above, so any callback
            # still in flight from an earlier fill is refused rather than putting a cursor back.
            choices.highlighted = None
            if focus:
                choices.focus()
        elif entries and focus:
            resting = min(highlight, len(entries) - 1)
            # Set twice, deliberately. Here, so the cursor is correct the instant this
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

        The counter is per screen now rather than per app, which is strictly tighter: a
        deferred placement can only ever be answered by a later fill of the *same* screen,
        and a screen that has been popped cannot be repainted by one at all.

        The highlight is cleared first because the fill has usually already assigned this exact
        value, and a reactive assigned its current value notifies nothing — so without the
        clear `watch_highlighted` would not run a second time and the deferred pass would
        achieve nothing at all. What that second run is *for*: the drawn cursor does not
        depend on it (`render_line` reads `highlighted` directly, so the value already set is
        on screen), but `scroll_to_highlight` does, and that is the part the first pass is too
        early to complete.

        `watch_highlighted` returns immediately on `None`, so the clear posts nothing —
        subscribers see one `OptionHighlighted` per fill, not a None-then-real pair.
        """
        if generation != self._resting_generation or not choices.options:
            return
        choices.highlighted = None
        choices.highlighted = index

    def text_entry(
        self,
        placeholder: str,
        *,
        validators: Sequence[Validator] = (),
        valid_empty: bool = True,
    ) -> None:
        """Hand the keyboard to the input, which only the text screens ever use.

        `validators` are Textual's, run on every change, and both of this surface's are
        wrappers that call the one function already holding the rule (`screens/validation.py`).
        Passing none leaves the entry unvalidated, which is right for the project filter: a
        query that matches nothing is a legitimate thing to have typed, not an error.

        `valid_empty` defaults to true because the entry it is most often shown for is the
        *optional* label, where empty means "skip" — the screen that requires a value says so.
        """
        if not self.showing:
            return
        self.show_choices(())
        entry = self.query_one("#filter", Input)
        entry.display = True
        entry.placeholder = placeholder
        # **Both of these are set before the value is cleared, and the order is load-bearing.**
        # `Input.value` is a reactive whose watcher validates the new value and posts `Changed`,
        # so assigning the value first would run whatever validators the entry was last given
        # under whatever `valid_empty` it was last given. Today that happens to be harmless —
        # the value is already `""` so the watcher does not fire, and Textual's own default for
        # `valid_empty` is `False`, which is what `NameScreen` wants anyway — but both of those
        # are coincidences of the current defaults rather than anything this code arranges. A
        # review found the whole "no rejection toast when the name entry opens" property
        # resting on them. Setting the contract first makes it arranged.
        entry.validators = list(validators)
        entry.valid_empty = valid_empty
        entry.value = ""
        # An entry the owner has not typed into yet is neither valid nor invalid, and it must
        # not open wearing the red border a previous screen's rejection left on the class list.
        entry.remove_class("-invalid", "-valid")
        self._last_rejection = None
        entry.focus()

    def announce_rejection(self, result: ValidationResult | None) -> None:
        """Say why the entry is refusing this value, once per distinct reason.

        Called from a screen's `on_input_changed`, so the owner is told at the keystroke that
        broke the rule rather than at the enter that submits it. The message is whatever the
        shared rule raised, so what is said while typing is word-for-word what used to be said
        on submit.

        **Deduplicated, and the dedup is the difference between telling and nagging.** Typing
        five characters past the label bound breaks the same rule five times; five identical
        toasts would bury the instruction the owner is still following under a stack of copies
        of one sentence. A new message is always announced, and the same one again is not —
        so correcting the value and breaking a *different* rule still speaks up.
        """
        if result is None or result.is_valid:
            self._last_rejection = None
            return
        message = "; ".join(result.failure_descriptions)
        if message == self._last_rejection:
            return
        self._last_rejection = message
        self.announce(message, severity="warning")

    def hide_entry(self) -> None:
        if not self.showing:
            return
        entry = self.query_one("#filter", Input)
        entry.value = ""
        entry.display = False

    def show_output(self, text: str) -> None:
        if not self.showing:
            return
        # One class, two rules. The pane appearing and the list disappearing are the same
        # fact — this screen is showing output — and asserting it twice imperatively is how
        # they could ever have disagreed.
        self.add_class("-showing-output")
        # `load_text` rather than the `text` setter only to say plainly that this replaces the
        # document and clears the edit history; the setter is documented as an alias for it.
        output = self.query_one("#output", TextArea)
        output.load_text(text)
        # **Hand the pane the keyboard, or the widget swap buys nothing.** A screen reaches
        # here through `show_choices(())`, which takes no focus because it has no rows, after
        # `hide_entry` has left the keyboard on an `Input` it just set `display = False` on. So
        # the focused widget was an invisible one, and `end`, `pagedown` and the arrows — the
        # whole reason this pane is a `TextArea` and not a `Static` — went nowhere. Measured
        # before the fix: `scroll y before/after keys: 0 0` on a 400-line capture.
        #
        # Focused here, in the shared path, rather than in `InspectScreen`: today that screen
        # is the pane's only caller, but the thing that must not be forgotten is the pairing —
        # revealing the pane and handing it the keyboard — and a second caller that forgot it
        # would reintroduce exactly this defect with nothing to catch it.
        #
        # Safe against the Back path: `TextArea._on_key` returns early on `read_only` *before*
        # the branch that consumes `escape` to move focus, so escape still reaches the app.
        output.focus()

    def hide_output(self) -> None:
        if not self.showing:
            return
        self.remove_class("-showing-output")

    # Interaction ---------------------------------------------------------------

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Route a chosen row to this screen, and to nothing above it.

        `event.stop()` is **defensive, not load-bearing today** — stated precisely because an
        earlier draft of this docstring claimed the opposite. This diff removed the app-level
        `on_option_list_option_selected`, and neither `App` nor `Screen` defines one, so there
        is no second handler in the ancestor chain for the message to reach and stopping it
        changes nothing observable right now.

        It is kept because the window where that stops being true is *this stage*: Task 2.4
        deletes the remaining dispatch chain, and an app-level handler reappearing during that
        work — even briefly — would otherwise see every row selection a second time, after the
        screen had already acted on it. On a destructive confirm that is a second dispatch of
        a keypress the owner pressed once.
        """
        event.stop()
        # Every row `show_choices` builds carries its key as the option's id, so a `None`
        # here means a row this app did not construct — refuse it rather than dispatch on it.
        key = event.option_id
        if key is None or self.tui.busy:
            return
        if key == _BACK:
            # **Handled here, once, because the failure paths render this row onto screens
            # that never asked for it.** `report_store_failure` and `fetch_page` both draw a
            # lone `_BACK` row onto whichever position's read failed — which can be any of
            # them — and a screen whose `choose` did not know about the key treated it as
            # data: the sessions list asked the store for a session called `\x00back`, the
            # project list answered "That project is no longer available. Refresh and try
            # again." Dead ends, on the path that runs when something is already broken, each
            # reporting a cause that is not the cause. Six screens had grown the same
            # three-line branch and three had not; BL-020 records the shape and says decide
            # once, which is what this is.
            #
            # The per-screen `_BACK` branches stay rather than being deleted as dead code,
            # and not out of caution: `choose` is called directly, by tests and by
            # `after_command`, without passing through this handler at all. Removing them
            # would make this the *only* route to Back, which is a narrower guarantee than
            # the one being added.
            #
            # Only `_BACK`. `_CANCEL` is left with the screens that render it, and the reason
            # is now weaker than it was: it used to have a genuine exception — the resume
            # confirmation meant "go back one step" by it where every other screen means
            # "unwind to the project list" — and that screen has since been removed, so the
            # surviving `_CANCEL` rows do agree. Hoisting it is therefore *possible* and is
            # deliberately not done here: it would be a second behaviour change riding along
            # with a removal, and the screens that render it are still the ones that know what
            # they mean by it.
            await self.tui.go_back()
            return
        await self.choose(key)

    async def redraw_after_failure(self) -> None:
        """Redraw this position after a command raised, moving the cursor off what failed.

        **A hook rather than a literal, because the right redraw depends on what the rows
        are.** Every failure path in `app.py` used to write `show_choices(((_BACK, "Back"),))`
        directly, and that is correct for a screen describing *one* record: it takes the
        cursor off the button that just failed, so a second enter cannot re-issue the command
        as a blind retry, and the one thing left to do really is go back.

        It is wrong for a screen whose rows are a **listing**. `show_choices` clears and
        redraws rather than appending, so the same call on the managed-sessions list replaces
        every row with a lone `Back` — hiding N-1 sessions that are working fine because one
        stop raised. That is the defect ask 6 was filed about, one level up and with a larger
        blast radius, and it became reachable the moment a listing started calling `tui.stop`.
        Found by the Stage 2 Tier-1 review.

        The default is the old literal, so every screen that was right stays byte-identical;
        `SessionsScreen` overrides it. Async because a listing has to re-read to redraw, and a
        hook that could not await would force the override back into the caller.

        **The status sentence moved in here with the redraw, and for the same reason.** It was
        written beside the caller as "Go back and open the session again to see its current
        state." — true on a detail, where the redraw has left a lone `Back` row and there is
        nothing else to do. On the *list* it tells the owner to go somewhere they already are,
        about a session whose current state the re-read has just put back on screen. Telling
        someone to navigate away from the position they are standing on is the defect ask 6 was
        reported for, and it survived the gate because the tests assert the toast rather than
        the status line. Found by the master close-out evaluator, driving both surfaces.
        """
        self.show_choices(((_BACK, "Back"),))
        self.set_status("Go back and open the session again to see its current state.")

    async def after_command(self) -> None:
        """What this screen does once a command it asked for has landed.

        Re-read in place, which is right for every screen that issues a command: the owner
        stays where they are and the rows are redrawn from the store.

        **Two screens reach this now, not one.** It was written when the session detail was
        the only caller of `tui.stop`, and its own wording said so; the sessions list became a
        second caller when `s`, `c` and `f` moved onto it. Nothing had to change — the
        implementation is `self.on_reveal()`, which each screen already defines as "read what
        I show again" — but a comment naming one caller is how the next reader concludes the
        other path does not come through here.

        **Nothing overrides this today, and the docstring here used to say something did.**
        The override was the two confirmations, which left themselves after issuing so the
        detail beneath came back refreshed. Both are `ModalScreen`s now, dismissed by the
        answer before the command is issued at all, so the only caller of this is the detail
        and the only implementation is this one.

        Kept rather than inlined because the question it asks — "am I a position the owner
        should stay on after this?" — is still the screen's own, and an earlier version that
        guessed it from stack depth popped the detail out from under a graceful stop. A seam
        with one implementation is cheap; re-deriving it from the stack is what was expensive.
        Worth revisiting in the presentation sub-plan if no second implementation appears.
        """
        await self.on_reveal()

    async def choose(self, key: str) -> None:
        """Act on the row the owner selected. Overridden by every concrete screen."""


class GatheredSelectionScreen(ChoiceScreen):
    """A review position whose work is a gathered selection rather than a typed entry.

    The last screen of such a flow holds everything the owner chose across the screens behind
    it, and holds none of it in a widget. Its entry was committed a screen ago and hidden, so
    the inherited `work_in_flight` would answer "nothing in flight" while a whole flow's worth
    of choices sat one keystroke from being discarded with no way back to them.

    **It has one subclass, `ProjectReviewScreen`, and that is the whole of the story rather
    than an erosion of it.** This was extracted when *two* screens answered identically — the
    project review and the launch review — with the same two properties and the same
    twelve-line argument copy-pasted between them. The launch review has since been removed
    outright: DEC-033 took the label step that gave it something to protect, and what was left
    held two list selections that escape gives back in two keystrokes. So the class is down to
    one subclass, which is one more than none, and it stays because the screen that needs it
    needs it. `ProjectReviewScreen` holds a *typed* project name that `NameScreen.populate`
    clears, so its work is still work escape cannot give back — which is the test of whether
    this class is a principled distinction or a convenient one.

    Two near-identical bodies are two chances for a later edit to fix one and
    miss the other, and the pair has to move together: `work_at_risk` is only correct for
    these screens *because* `work_in_flight` is unconditionally true, and a screen that
    changed one without the other would either warn about nothing or fail to warn at all.

    `work_at_risk` is deliberately the empty string rather than a summary of the selection.
    The quit warning has a sentence for work it cannot name, and inventing one here would mean
    keeping a rendering of the gathered state in step with the screens that gather it — a
    second place to drift, to say something the owner can already see on the screen they are
    looking at.
    """

    @property
    def work_in_flight(self) -> bool:
        return True

    @property
    def work_at_risk(self) -> str:
        return ""
