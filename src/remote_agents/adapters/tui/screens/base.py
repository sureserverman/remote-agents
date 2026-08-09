"""The body every choice screen renders, and the one fill path all of them share.

This is where `RemoteAgentsTui._fill` moved. Keeping a single choke point is not tidiness:
`show_choices` deduplicates row keys, and the Sub-plan 1 handoff records why that guard has
to live on the shared path rather than at the one call site that needs it today — a screen
fed by an external source must not be able to reintroduce the crash by forgetting a guard.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.notifications import SeverityLevel
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, OptionList, Static
from textual.widgets.option_list import Option

if TYPE_CHECKING:
    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.context import TuiContext

_LOG = logging.getLogger(__name__)

#: The three app bindings that leave the current flow entirely — each unwinds the stack to the
#: resting position, which is what makes them able to discard a half-typed value.
_FLOW_JUMPS = frozenset({"add_project", "sessions", "resume"})


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
        if "\n" in cls.status:
            raise ValueError(
                f"{cls.__name__}.status must be one line; the status region is one line high"
            )

    def __init__(self) -> None:
        super().__init__()
        # Bumped by every fill; a deferred cursor placement carries the value it was
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
                yield Static("", id="output", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        """Set the chrome this screen asked for, then let it fill itself.

        A template method rather than an overridable `on_mount`, so a screen cannot forget
        the chrome by defining its own handler — the output pane in particular starts hidden
        on every screen and used to be reset from a single place in the app.
        """
        self.show_breadcrumb()
        self.query_one("#output-pane").display = False
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
        deliberate.** The three flow jumps unwind the stack, so pressing one while a label is
        half-typed — or while a whole selection is gathered and waiting at the review step —
        discards it with no warning and no way back. The action has no early return for that
        because the action never knew. This is the rule that had to be invented rather than
        mirrored, and it is the only one answering `None`: the key stays drawn and greyed rather
        than vanishing, because a footer entry that disappears as the owner types would be a
        second surprise on top of the first.

        **Quit is deliberately not in that set, and saying so is the point.** `ctrl+q` discards
        in-flight work exactly as the three jumps do, and a stage review reproduced it: type a
        label, press it, the app is gone and the label with it. It is left enabled because the
        two keys mean different things to the person pressing them. The jumps mean "go somewhere
        else in this app", and losing the work is a side effect nobody asked for; quit means
        "leave", and an app that refuses to close until an entry is cleared is a worse answer
        than the one it replaces. What it *should* have is a warning rather than a refusal —
        recorded as BL-025, and it belongs with the notification work rather than here, because
        the surface currently has nowhere to put such a warning.
        """
        if action == "back":
            # `go_back` refuses to pop the last screen, so at the resting position escape is
            # inert by construction — see `RemoteAgentsTui.go_back`.
            return False if len(self.app.screen_stack) <= 1 else True
        if action == "refresh":
            # Mirrors `action_refresh`, which now delegates to `refresh_contents`.
            return True if self.can_refresh else False
        if action == "resume" and self.services.conversations is None:
            # Mirrors `action_resume`'s `self._services.conversations is None` early return: a
            # host that wired no conversation service has no resume flow to open, and the
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
        found what it missed. A label typed on `LABEL` is protected while it is being typed and
        then *committed*, at which point the box is empty and the same three keys throw away the
        whole gathered selection from the review step one screen later. The thing worth
        protecting was never the widget's contents; it is work the owner cannot get back by
        pressing escape.

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
        entry = self.query("#filter").first(Input) if self.query("#filter") else None
        return bool(entry is not None and entry.display and entry.value)

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

        Guarding here rather than at each call site is deliberate. A sweep of this package
        found eleven methods that await and then render or push; adding a caller-side guard to
        each is precisely the arrangement that let two of them be missed when their four
        siblings got one.
        """
        return self.app.screen is self

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
        saying: `ReviewScreen`'s own line reads "Label: none. Launch, or go back." — an
        instruction to press a button that is at that moment covered and refusing input. A
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
            self._set_working(False, previous)

    def _status_text(self) -> str:
        """What the status line currently reads, so `awaiting` can put it back.

        `str(...)` rather than `.plain`, because `Static.content` is a `str` on a widget built
        with `markup=False` and a `Content` on one built without it — and `#status` is the
        former. Reading it as though it were always a `Content` is an `AttributeError` on the
        one configuration this app actually uses.
        """
        return str(self.query_one("#status", Static).content) if self.showing else ""

    def _set_working(self, working: bool, text: str) -> None:
        if not self.showing:
            return
        self.query_one("#choices", OptionList).loading = working
        self.set_status(text)

    async def advance_to(self, screen: Screen[None]) -> None:
        """Push `screen`, unless this one has been left while a read was in flight.

        The other half of `showing`. A push from a screen that is no longer on top lands on
        whatever replaced it — which is how an output pane ends up sitting above a position
        that is not showing the session whose output it captured.

        The example used to be the force confirmation, which was pushed through here. It is a
        `ModalScreen` awaited through `ask_to_confirm` now and does not come this way at all,
        so the hazard is the same and the illustration had to change with the call sites: the
        inspect screen and the three resume pushes are what still travel through this.
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

    def set_status(self, text: str) -> None:
        """Say, in one line, what to do here or what just happened.

        One line is a *contract*, not a suggestion the CSS enforces: `#status` is one line
        high, so a second line is not clipped visibly, it is invisible. The first line is
        rendered and the whole value is logged, which turns a silent loss into one that can be
        found — and the log is not the fallback nobody reads, because a static check over this
        package's own call sites (`test_status_region.py`) fails on a literal that contains a
        newline. The runtime guard is what catches the values a static check cannot see: an
        exception's `str()` carries newlines from the library that raised it, and every
        failure path in this surface interpolates one.
        """
        if not self.showing:
            return
        if "\n" in text:
            _LOG.warning("a multi-line status was truncated to its first line: %r", text)
            text = text.split("\n", 1)[0]
        self.query_one("#status", Static).update(text)

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
        surface, that `markup=False` on the two Statics was added for. Escaping at each call
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
        self, entries: tuple[tuple[str, str], ...], *, focus: bool = True, highlight: int = 0
    ) -> None:
        """Render the choices, and take the keyboard only when the list is the next decision.

        Refilling while the owner is typing a filter must leave the keyboard where it is, or
        every character after the first lands on the list instead of the query.
        """
        if not self.showing:
            return
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
        choices.add_options(Option(text, id=key) for key, text in entries)
        if entries and focus:
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

    def text_entry(self, placeholder: str) -> None:
        """Hand the keyboard to the input, which only the text screens ever use."""
        if not self.showing:
            return
        self.show_choices(())
        entry = self.query_one("#filter", Input)
        entry.display = True
        entry.value = ""
        entry.placeholder = placeholder
        entry.focus()

    def hide_entry(self) -> None:
        if not self.showing:
            return
        entry = self.query_one("#filter", Input)
        entry.value = ""
        entry.display = False

    def show_output(self, text: str) -> None:
        if not self.showing:
            return
        self.query_one("#output-pane").display = True
        self.query_one("#choices").display = False
        self.query_one("#output", Static).update(text)

    def hide_output(self) -> None:
        if not self.showing:
            return
        self.query_one("#output-pane").display = False
        self.query_one("#choices").display = True

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
        await self.choose(key)

    async def after_command(self) -> None:
        """What this screen does once a command it asked for has landed.

        Re-read in place, which is right for the session detail issuing any of its commands:
        the owner stays where they are and the rows are redrawn from the store.

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
