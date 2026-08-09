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
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, OptionList, Static
from textual.widgets.option_list import Option

if TYPE_CHECKING:
    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.context import TuiContext

_LOG = logging.getLogger(__name__)


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

    #: Shown in the status line until the screen replaces it.
    status = ""
    #: When set, the filter input is visible and carries this placeholder.
    filter_placeholder: str | None = None
    #: The name this position is committed under in the snapshot baselines. Declared on the
    #: screen rather than mapped in the test so that adding a screen and forgetting its
    #: baseline is a missing name here, not a silently uncovered position there.
    position = ""

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

    def set_status(self, text: str) -> None:
        if not self.showing:
            return
        self.query_one("#status", Static).update(text)

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
