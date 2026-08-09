"""The body every choice screen renders, and the one fill path all of them share.

This is where `RemoteAgentsTui._fill` moved. Keeping a single choke point is not tidiness:
`show_choices` deduplicates row keys, and the Sub-plan 1 handoff records why that guard has
to live on the shared path rather than at the one call site that needs it today — a screen
fed by an external source must not be able to reintroduce the crash by forgetting a guard.
"""

from __future__ import annotations

import logging
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

    # Rendering -----------------------------------------------------------------

    @property
    def tui(self) -> RemoteAgentsTui:
        """The app, typed. Screens reach the services and shared selection through it."""
        return cast("RemoteAgentsTui", self.app)

    @property
    def services(self) -> TuiContext:
        return self.tui.services

    def set_status(self, text: str) -> None:
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
        self.show_choices(())
        entry = self.query_one("#filter", Input)
        entry.display = True
        entry.value = ""
        entry.placeholder = placeholder
        entry.focus()

    def hide_entry(self) -> None:
        entry = self.query_one("#filter", Input)
        entry.value = ""
        entry.display = False

    def show_output(self, text: str) -> None:
        self.query_one("#output-pane").display = True
        self.query_one("#choices").display = False
        self.query_one("#output", Static).update(text)

    def hide_output(self) -> None:
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

    async def choose(self, key: str) -> None:
        """Act on the row the owner selected. Overridden by every concrete screen."""
