"""Listing managed sessions, one session's detail, and its captured output.

Three screens replacing three wizard positions. The session id the detail renders was one of
the seven navigation fields the app used to carry; it is `SessionDetailScreen.session_value`
here, so the detail cannot be rendered for a session the screen was not opened with, and no
other flow can leave a stale id behind for it to read.

Both destructive confirmations live in `screens/confirm.py` as `ModalScreen[bool]`s awaited
through `ask_to_confirm`, so the answer comes back to the method that asked and no app-level
binding can walk away from the question. What that changes here is where the decision lives:
`confirm_force` and `confirm_remote_control` read the answer and issue the command themselves,
rather than handing the session id to a screen that issued it on its own. It is also why the
detail offers Enable and Disable as separate rows — a confirmation answered with a bool has to
be asked about one direction.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from types import MappingProxyType

from textual import events
from textual.binding import Binding
from textual.content import Content
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Input, OptionList, TextArea

from remote_agents.adapters.tui.model import _BACK, label_or_error
from remote_agents.adapters.tui.rows import session_contents, session_counts_content
from remote_agents.adapters.tui.screens.base import NEVER_EMPTY, ChoiceScreen, held_option_id
from remote_agents.adapters.tui.screens.confirm import (
    ForceConfirmModal,
    RemoteControlConfirmModal,
)
from remote_agents.adapters.tui.screens.validation import LabelWithinBound
from remote_agents.application.captures import render_capture
from remote_agents.application.session_actions import (
    ACTION_LABELS,
    CLEANUP,
    FORCE,
    GRACEFUL,
    REMOTE_CONTROL_LABELS,
    available_actions,
    explain_state,
    remote_control_available,
    remote_control_directions,
)
from remote_agents.application.session_views import session_row_parts
from remote_agents.domain.models import SessionId, SessionRecord
from remote_agents.domain.remote_control import RemoteControlState

_LOG = logging.getLogger(__name__)

_INSPECT_MAX_LINES = 2000
_INSPECT_MAX_BYTES = 512 * 1024

#: This surface's row key for each Remote Control direction, and nothing else. The table used
#: to carry the labels too, which made it a second source of truth for strings the shared
#: `REMOTE_CONTROL_LABELS` now owns — two places to change, one of which the parity contract
#: would not have caught, since it compares what each surface *renders* rather than what each
#: surface stores. A row still cannot exist without a direction behind it: the key is derived
#: from the state rather than sitting beside it.
_REMOTE_CONTROL_DIRECTIONS = {
    "remote-control-active": RemoteControlState.ACTIVE,
    "remote-control-inactive": RemoteControlState.INACTIVE,
}
_REMOTE_CONTROL_KEYS = {state: key for key, state in _REMOTE_CONTROL_DIRECTIONS.items()}


def remote_control_entries(record) -> tuple[tuple[str, str], ...]:
    """The (key, label) rows this surface offers for Remote Control, from the shared policy.

    Module-level and named rather than inlined, so the parity contract can read exactly what
    the screen renders without driving a Textual app to find out.
    """
    return tuple(
        (_REMOTE_CONTROL_KEYS[direction], REMOTE_CONTROL_LABELS[direction])
        for direction in remote_control_directions(record, record.remote_control_state)
    )


#: How often the sessions list re-reads the store while it is the screen on top. Long enough
#: that a host is not answering a tmux readiness probe continuously, short enough that a
#: session another process started is visible before the owner thinks to press Ctrl+R.
_SESSIONS_AUTO_REFRESH = 10.0


class RowStopAction(Message):
    """A stop the owner asked for, delivered to the receiving screen's own pump.

    **Two senders and any `ChoiceScreen` receiver, since the Alt layer.** A row key on the
    sessions positions sends it with the session under its cursor; a chord sends it from any
    console pane with the console's published selection. The handler is on `ChoiceScreen` so
    both land on the pump of the screen the owner is looking at.

    **Posted rather than performed inline, and the reason is a deadlock that shipped in this
    stage before a gate evaluator drove the real surface.** Textual dispatches a *screen's*
    binding from `App._on_key` -> `_check_bindings` -> `run_action`, so a binding action runs
    on the **App's** message-pump task, not the screen's. `confirm_force` suspends there on
    `ask_to_confirm`, the app stops draining messages, and nothing gets through again: measured
    on the owner's real workstation, the modal drew correctly and then Escape did nothing,
    `ctrl+q` did nothing, and the process had to be killed. `SessionsPaneScreen` inherits the
    binding, so the console pane froze the same way.

    The detail's force was never exposed to this and that is what showed the way out: it is
    reached from `on_option_list_option_selected`, a *message handler on the screen*, which
    runs on the screen's own pump — so a suspension holds back only that screen's events. That
    is exactly the protection DEC-025 describes, and `OpeningAction` below already exists
    because the same decision forced the same shape on the detail's opening actions.

    **DEC-025's architecture sweep could not catch this**, which is worth recording where the
    fix lives: it asserts the caller is a method on a `*Screen` class, and `confirm_force` is
    one. What the decision actually requires is a property of the *pump the call arrives on*,
    which no static check of the call site can see. The sweep is unchanged and still worth
    having — it catches workers, timers and app-level bindings — and this note is the part it
    cannot enforce.

    Recorded as **DEC-068**, which extends DEC-025 to the caller its own list missed and states
    the sweep's limit. DEC-027 covers the app-level case and keeps the modal for a destructive
    action; this is how force keeps it without suspending the application.
    """

    def __init__(self, action: str, session_value: str) -> None:
        super().__init__()
        self.action = action
        self.session_value = session_value


class OpeningAction(Message):
    """One action for a freshly-opened session detail to perform on arrival.

    **A message rather than `call_after_refresh`, and DEC-025 is the whole reason.** The
    dispatch cannot be awaited inside `populate` -- `ChoiceScreen.on_mount` awaits that, so a
    confirmation raised there waits for a pump that has not started, and the app deadlocks
    (observed: a test that hung rather than failed). The obvious repair is to defer it onto a
    scheduled callback, and that is precisely what DEC-025 forbids: a callback handed to
    `call_after_refresh` is on the decision's list of callers whose suspension does not hold
    the pump, so a modal raised from one could be popped out from under its own `await` and
    hang forever holding whatever the caller held. `tests/architecture/
    test_confirmations_are_asked_from_screen_handlers.py` fails on exactly that, and it did.

    A posted message is neither. It is delivered to `SessionDetailScreen.on_opening_action`,
    an ordinary screen handler running on the screen's own message pump -- which is the shape
    DEC-025 says makes every other confirmation in this tree safe, and the shape the failing
    test's own remedy names: "move the call onto a screen handler".
    """

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action


#: One key per action the session detail offers, and the action each one names.
#:
#: **Bare letters, which is affordable here and nowhere else on this surface.** Both sessions
#: positions call `hide_entry()`, so there is no filter to type into and a letter cannot be
#: mistaken for a search. The projects pane cannot do this: its filter holds the keyboard by
#: construction, which is why Stage 5's order toggle has to be a `ctrl+` key.
#:
#: Nothing here decides whether an action is *legal*. The key names it, the policy re-checked at
#: issue time is what refuses -- DEC-007's fourth mitigation -- and a key is only ever a faster
#: way to reach something a row already offers.
#:
#: **Where it is performed depends on what the key is for, and that split is ask 6.** `a`, `i`
#: and `r` open the detail and it performs them through `choose`. `s`, `c` and `f` act on *this*
#: list: they used to push a detail nobody asked for and run the action there, which left the
#: owner on a screen they had not chosen — and after a graceful stop that worked, on that
#: screen's "That session is no longer available." above a lone Back row. See
#: `action_row_action`, which carries the full account.
#:
#: That chain asks before `force` and before either Remote Control direction, and does **not**
#: ask before Stop and close or Clean up. Stated precisely rather than as "the same
#: confirmations a row gets", which was the first version of this comment and was the sentence
#: a later reader would have used to conclude this path was already safe for all six.
#:
#: **`s` and `c` carry those two unconfirmed actions, and that is DEC-052 amended rather than
#: ignored.** The original decision banned them at import, on two premises. The first still
#: holds and is untouched: DEC-018 says graceful stop and cleanup are not confirmed on either
#: surface, so these keys do not ask and the bot is not changed. The second was the load-
#: bearing one — this list restores its cursor *by key* every ten seconds and fell back to row
#: 0 when the key had gone, so a session ending between ticks moved the cursor onto a
#: different session in silence, and one keypress there would have stopped an agent the owner
#: never selected. That fallback is gone: `_draw_listing` now rests the cursor on *nothing*
#: when the row it was holding disappears, which was DEC-052's own rejected alternative 3 and
#: which it called "a genuine improvement". A key cannot act on a row the owner did not put
#: the cursor on, because after such a refresh there is no row under the cursor at all.
#:
#: What is left is the residual DEC-052 named and DEC-018 already accepted: one keypress, on a
#: row the owner *is* looking at, irreversible and unasked. That is the owner's standing trade
#: for graceful stop everywhere else on both surfaces, not a new one taken here.
#:
#: **The `\u25b8` marker is what that residual bought a mitigation for**, and it is a rendering
#: rather than a second selection: the row these keys act on is drawn marked and yellow, so
#: "which session is one keypress from ending" is legible -- including from the panes that
#: carry the Alt layer and cannot see this cursor at all. A design that made the marker a
#: *separate*, committed selection was built and reverted; two answers to "which session" cost
#: more confusion than the scrolling hazard it removed, and DEC-062 had already accepted that
#: hazard on the owner's behalf.
#:
#: The one thing that moves this cursor without a keypress is a session *starting*, which is
#: the owner naming it by starting it -- `_newest_arrival` carries that argument and its
#: residual in full.
#:
#: The fourth field is the word a one-line region has room for, and it is not the action id.
#: `graceful` is the lifecycle's name for the action; "Stop and close" is the owner's, and
#: `session_actions.ACTION_LABELS` is emphatic that the second is what a surface shows. A
#: line reading "s graceful" would put the mechanism back on screen in the one place the
#: label was written to keep it off. `stop` and `clean` are those labels at that width. Since
#: the redesign the list advertises the bare letters in its title and the word is one `d` away;
#: the field is kept because it is the pinned owner-vocabulary for the day a region wants it.
#:
#: No trust key, deliberately (DEC-047): this surface answers the trust question in the pane
#: the console exchanges in, so it has no trust row and must not grow a trust key either.
SESSION_ACTION_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("a", "attach", "Copy attach", "attach"),
    ("i", "inspect", "Inspect output", "inspect"),
    ("r", "rename", "Rename", "rename"),
    ("s", GRACEFUL, ACTION_LABELS[GRACEFUL], "stop"),
    ("c", CLEANUP, ACTION_LABELS[CLEANUP], "clean"),
    ("f", FORCE, ACTION_LABELS[FORCE], "force"),
)

#: The actions a key carries **without asking**: exactly the branch of
#: `SessionDetailScreen.choose` that reaches `tui.stop` with no modal in between --
#: `key in ACTION_LABELS and key != FORCE`, which today is Stop and close, and Clean up.
#: Derived from that condition rather than listing the two, so a third unconfirmed action added
#: to the policy is described here the day it appears.
#:
#: This set used to be a *ban list*, enforced by a `raise` a few lines below: DEC-052 held that
#: no key could carry an unconfirmed mutating action at all. The ban is lifted and the set
#: kept, because what it names is still the thing that needs guarding -- it is now the reason
#: `_draw_listing` may not fall back to row 0, rather than the reason `s` and `c` may not
#: exist. See `SESSION_ACTION_KEYS` for the amendment and the premise that changed, and
#: `_draw_listing` for the mitigation that replaced the ban.
UNCONFIRMED_MUTATING_ACTIONS = frozenset(ACTION_LABELS) - {FORCE}

#: Whether `_draw_listing` leaves the cursor on nothing when the row it was holding has gone,
#: instead of falling back to row 0.
#:
#: A constant rather than something inferred, because the thing it describes is a branch in a
#: method and no import-time check can read one. It is set beside that branch's own module and
#: asserted against the real behaviour by
#: `test_a_vanished_row_leaves_the_cursor_on_nothing`, so a reader who edits `_draw_listing`
#: and forgets this flag is caught by a test, and a reader who flips this flag without editing
#: `_draw_listing` is caught by the same one. What the flag buys that the test cannot is the
#: import-time refusal below, which fires before a surface with live stop keys can start.
_CLEARS_VANISHED_CURSOR = True

#: The Remote Control key, kept out of the table above because it is the one key whose action
#: is not known until the record is read -- see `action_row_remote_control`.
_REMOTE_CONTROL_KEY = "m"

#: The row keys as the pane title advertises them -- `Sessions 6 · a i r s c f m` -- built from
#: the table rather than written beside it. Both sessions positions carry the title and the
#: second is a subclass of the first, so a literal in each would be two strings to keep agreeing.
#:
#: The title, not the status line, since the redesign: the status carries the counts (`● 2
#: running · …`) and the muted hint row beneath it the navigation keys, so the letters moved to
#: the frame of the list they act on -- which is also where they stop competing for the columns
#: the old three-row status needed at 60 wide. Letters alone; the word for each is one `d` away
#: on the detail, and the modal that asks before `f` names its action in full.
ROW_KEY_LETTERS = " ".join(
    [*(key for key, _action, _label, _word in SESSION_ACTION_KEYS), _REMOTE_CONTROL_KEY]
)


def sessions_title(count: int) -> str:
    """The list's border title: the count, then the row keys muted. Markup on fixed text only."""
    return f"Sessions {count}[$text-muted] · {ROW_KEY_LETTERS}[/]"


#: The bindings themselves, built once from the table above.
#:
#: **Declared here and attached to the screen classes, not to the mixin below**, and that is a
#: fact about Textual rather than a preference: `BINDINGS` are merged across the MRO for
#: `DOMNode` subclasses only, so a plain mixin's list is silently skipped. The first version of
#: this task put them on the mixin, and the screen reported exactly `['d']` -- no error, no
#: warning, every new key simply inert. Measured, then moved.
SESSION_ACTION_BINDINGS = [
    # Hidden from the footer for the same reason `d` is: the bar is shared with every
    # inherited binding, and seven more entries would clip the ones the owner did not ask
    # for. Task 4.3 is what keeps the hidden ones honest.
    *(
        Binding(key, f"row_action('{action}')", label, show=False)
        for key, action, label, _word in SESSION_ACTION_KEYS
    ),
    Binding(_REMOTE_CONTROL_KEY, "row_remote_control", "Claude Remote Control", show=False),
]

# The invariant that replaced DEC-052's ban, enforced where it cannot be skipped -- the same
# `raise`-at-import pattern, now guarding the mitigation instead of the prohibition.
#
# An unconfirmed mutating key is safe only while a vanished cursor row rests on *nothing*: that
# is the whole of the argument for lifting the ban, and it lives in `_draw_listing`, four
# hundred lines away and in a method whose name says nothing about stop keys. Someone
# "restoring DEC-007's resting cursor" there would silently re-open the hazard -- one keypress
# stopping an agent the owner never selected -- and every test of the keys themselves would
# still pass. So the two are tied together here: bind an unconfirmed mutating action and the
# module refuses to import unless the fallback is gone.
_bindable = {action for _key, action, _label, _word in SESSION_ACTION_KEYS}
if _bindable & UNCONFIRMED_MUTATING_ACTIONS and not _CLEARS_VANISHED_CURSOR:
    raise RuntimeError(  # pragma: no cover - import-time invariant
        "these keys perform an action the detail never asks about: "
        f"{sorted(_bindable & UNCONFIRMED_MUTATING_ACTIONS)}. "
        "DEC-018 forbids confirming them, so the only thing making them safe is that "
        "`_draw_listing` rests the cursor on nothing when the row it held has gone. "
        "Restore that flag with the row-0 fallback, or drop these keys."
    )
del _bindable

#: The console key, kept off `SESSION_ACTION_BINDINGS` deliberately -- see
#: `SessionsPaneScreen.BINDINGS`, which is the only position that offers it.
#:
#: `F12` is named beside it in the status line rather than here: the root key is tmux's and
#: reaches the console from *inside a displayed agent*, which is the one place this screen's
#: own bindings cannot be pressed at all.
_SHOW_PROJECTS_BINDING = Binding("p", "show_projects_pane", "Projects", show=False)


#: The key `d` carries on the sessions pane: open this row's detail, with no action attached.
#:
#: Not in `SESSION_ACTION_KEYS` because it performs nothing on the session — that table's
#: fourth field is the word a *lifecycle* action is called by, and "look at it" is not one.
#: Named here because the chord layer carries it, and a literal spelled in two places is the
#: drift `test_the_chord_layer_is_the_row_keys.py` exists to catch.
_DETAIL_KEY = "d"

#: Every key the Alt layer offers, built from the tables rather than written beside them.
#:
#: **This is the whole of the chord vocabulary**, and it is derived so that a seventh row key
#: becomes a seventh chord with no second edit. `RemoteAgentsTui.BINDINGS` builds one
#: `alt+<letter>` binding per entry; the DEC-052 import guard above therefore covers the chords
#: by construction, because a chord cannot exist for a key this table does not carry and this
#: table cannot carry an unconfirmed mutating key while `_CLEARS_VANISHED_CURSOR` is false.
CHORD_KEYS: tuple[str, ...] = (
    *(key for key, _action, _label, _word in SESSION_ACTION_KEYS),
    _REMOTE_CONTROL_KEY,
    _DETAIL_KEY,
)

#: The Alt layer as a pane advertises it, built from the chord table so the row of letters the
#: owner reads is the row of letters that works.
#:
#: `⌥` rather than `alt`: it is the key's own glyph, it costs one column instead of three on a
#: hint row that is already sharing a line with the pane's own keys, and it is what the owner's
#: keyboard is labelled. The letters are spaced exactly as the sessions pane's title spaces them
#: (`ROW_KEY_LETTERS`), so the two readings of the same set look like the same set.
CHORD_HINT = "⌥ " + " ".join(CHORD_KEYS)


def chord_hint_content(base: str, *, live: bool) -> Content:
    """The hint row for a console pane: its own keys, then the Alt layer, dim when it is inert.

    **Two emphases on one line, which is why this returns `Content` rather than a string.** The
    row is `$text-muted` already; the chords go one step further to `$text-disabled` when the
    sessions pane's cursor rests on nothing, because in that state every one of these keys warns
    and does nothing (DEC-027). A key that is drawn identically whether or not it will work is
    the "dead-end key" complaint this stage keeps refusing elsewhere -- offering it and greying
    it is the honest middle, since what is missing is a *selection* rather than the capability.
    """
    keys = (CHORD_HINT, None if live else "$text-disabled")
    if not base:
        return Content.assemble(keys)
    return Content.assemble((base, None), (" · ", None), keys)


class ChordHintRow:
    """The hint row's account of the Alt layer, for a pane whose own keys are not the row keys.

    Mixed into the console panes that carry a cursor over something other than sessions. Today
    that is the projects pane and the feed; the limits pane is deliberately excluded and says so
    in its own CSS, and `DashboardScreen` inherits this by subclassing `ProjectsPaneScreen`
    without being a console pane at all -- which is why `advertises_chords` asks about the
    position rather than trusting the mixin's presence.

    The sessions pane is not one of them either: its title already advertises the same letters
    bare (`sessions_title`), and saying them twice on one small pane, once with a modifier and
    once without, would describe two key sets where there is one.

    **The live/dim state is cached rather than read at render time.** Rendering is synchronous
    and the answer is a tmux read, so the read happens on the pane's own reload cycle and this
    holds what it last learned. The cost of the cache is a hint that can lag the other pane's
    cursor by one tick; the cost of not having one would be a `show-options` on every redraw of
    every pane, and a hint row that cannot be drawn without awaiting.
    """

    #: The pane's own keys, which the chords are appended to. Empty on a pane that has none.
    chord_hint_base: str = ""

    #: What the last read found. `False` until one has happened, so a pane that has never read
    #: draws the layer dim rather than promising something it has not checked.
    _chord_live: bool = False

    #: This pane's own timer for the read above, or `None` off a console. Its own rather than
    #: borrowed, because the panes that carry this hint do not all reload anything: the limits
    #: and feed panes poll their own content, the projects pane polls nothing at all, and the
    #: fact being watched belongs to none of them -- it is the *other* pane's cursor.
    _chord_timer: Timer | None = None

    def start_chord_hint(self) -> None:
        """Take the first reading and keep it current. Called from a pane's `populate`.

        Same cadence as the sessions pane's own reload, deliberately: what this watches is that
        pane's cursor, so reading it faster would only find the same answer sooner than the
        thing being watched can change it.
        """
        if not self.advertises_chords() or self._chord_timer is not None:
            return
        self._chord_timer = self.set_interval(_SESSIONS_AUTO_REFRESH, self._chord_hint_tick)
        self.call_after_refresh(self._chord_hint_tick)

    async def _chord_hint_tick(self) -> None:
        if self.showing:
            await self.refresh_chord_hint()

    def advertises_chords(self) -> bool:
        """Whether *this* position should draw the layer — which is not "is this a console".

        **One screen inherits this mixin without being a console pane, and it is the one that
        must not draw the row.** `DashboardScreen` subclasses `ProjectsPaneScreen`, so it
        inherits the hint; but it owns a sessions cursor and binds none of the row keys, so
        `_offers_chords` refuses it every chord (that refusal was Task 3.1's Critical). The
        mixin's own carriers are the projects pane and the feed — the limits pane was removed
        from it when its `#hint { display: none; }` came to light.

        Drawing a lit `⌥ a i r s c f m d` there would advertise eight keys
        the app answers `False` for, two of which are unconfirmed stops — the dead-end key this
        stage keeps refusing, in its worst form: not merely inert, but inert *and* about
        stopping agents.

        So the row mirrors the layer's own gate rather than the hosting: a position that owns a
        sessions cursor draws no chord hint, because either it carries the bare letters already
        (and its title says so) or it is refused the chords entirely.
        """
        return self.tui.services.console_holds_slot is not None and not getattr(
            self, "owns_session_cursor", False
        )

    def hint_content(self, base: str) -> str | Content:
        if not self.advertises_chords():
            # Not a console pane, so there is no layer to advertise. `hosting_mode` gates the
            # capability, so its absence is the declared absence of the whole feature (DEC-046)
            # -- exactly the condition `check_action` uses to refuse the chords themselves.
            return base
        return chord_hint_content(base, live=self._chord_live)

    async def refresh_chord_hint(self) -> None:
        """Re-read whether anything is selected, and redraw the row if the answer changed.

        Asks `selected_session`, not the raw option: on the positions that draw this row it is
        the same question the chord asks, so a pane whose slot mark says it may not read the
        selection draws the layer dim rather than bright-and-refused. (It is *not* the same
        question on a cursor-owning screen, which resolves from its own list -- one more reason
        `advertises_chords` keeps this row off those positions.)

        Guarded like every other post-await continuation here: this runs from a timer, and the
        owner can leave between the read and the redraw.
        """
        if not self.advertises_chords():
            return
        try:
            live = await self.tui.selected_session() is not None
        except Exception:  # pragma: no cover - `selected_session` catches its own
            return
        if not self.showing:
            # Stored *and* returned would leave the row claiming whatever it last drew while
            # every later tick compares equal and never repaints -- on the feed pane nothing
            # else redraws the hint, so it would say so until the answer changed again.
            return
        if live == self._chord_live:
            return
        self._chord_live = live
        self.set_hint(self.hint_content(self.chord_hint_base))


#: Which action each row key names. The chord layer arrives holding a *key*; the row bindings
#: arrive holding an *action*, because that is what `Binding` was given. One mapping, so the
#: two entry points cannot disagree about what `s` means.
_KEY_ACTIONS = {key: action for key, action, _label, _word in SESSION_ACTION_KEYS}


async def perform_row_action(action: str, session_value: str, *, screen: ChoiceScreen) -> None:
    """Do what a row key names, to one named session, from whichever screen pressed it.

    **Module-level and taking its session explicitly, because two entry points reach it.** The
    row key on the sessions pane resolves the session from its own cursor; the Alt chord
    resolves it from the console's published selection, from a pane with no sessions list at
    all. What happens next has to be the same code, or "the chord does what the key does" is a
    claim maintained by hand in two bodies that drift.

    Nothing here checks the policy, and deliberately: an action the policy no longer allows is
    refused by the policy itself, in its own words, rather than by a check kept here that could
    drift from it (DEC-007's re-read at issue time, which lives in `RemoteAgentsTui.stop`).
    """
    if action in ACTION_LABELS:
        # **Posted, not performed**, and for all three keys rather than only for force.
        # A binding action runs on the *App's* pump (see `RowStopAction`), so anything
        # that suspends here suspends the whole surface: force deadlocked it outright on
        # the modal, and `s`/`c` blocked it for the duration of the stop, queueing the
        # owner's keystrokes and replaying them afterwards onto whatever screen had by
        # then arrived. Handing the work to `on_row_stop_action` puts every one of them on
        # this screen's own pump, which is where the detail has always run them.
        #
        # `screen` is the receiving screen rather than always the sessions pane, which is what
        # makes the chord obey DEC-025/DEC-068 from every pane: the message lands on the pump
        # of the screen the owner is looking at, and the handler there is what asks for FORCE.
        screen.post_message(RowStopAction(action, session_value))
        return
    await screen.tui.show_detail(session_value, action)


async def perform_row_remote_control(
    session_value: str, *, screen: ChoiceScreen, mark_excursion: bool = False
) -> None:
    """Remote Control, which is the one key that cannot name its action in advance.

    Shared by the row key and the chord for the reason `perform_row_action` states. The busy
    and cursor guards stay with the callers, because each resolves its session differently and
    the refusal belongs beside the resolution.
    """
    try:
        record = await screen.tui.current_record(session_value)
    except Exception as error:
        screen.tui.report_store_failure(error, screen)
        return
    if not screen.showing:
        # The owner left while the store was answering. Every other post-await continuation
        # on this screen family re-checks this before acting -- `dispatch_opening`,
        # `confirm_force`, `confirm_remote_control`, `show_attach` -- because `action_back`
        # only consults the app-level busy flag, and this method sets none. Without it a
        # read landing late pushes a detail onto whatever the owner navigated to instead.
        return
    if record is None:
        if mark_excursion:
            screen.mark_excursion()
        await screen.tui.show_detail(session_value)
        return
    if not remote_control_available(record):
        # The re-read at issue time, which is DEC-007's third mitigation, applied to the
        # one key whose availability `check_action` can only answer from the drawn row.
        # A session that stopped -- or a row the cursor moved onto between the redraw and
        # the keypress -- is refused here in words rather than by being navigated
        # somewhere. `show_detail` was what this did, and a detail the owner did not ask
        # for is not a refusal, it is a refusal-shaped move.
        screen.announce("Remote Control is only for a running Claude session.", severity="warning")
        return
    # The direction is picked from this read and re-checked by `confirm_remote_control`'s
    # own read -- which asks whether Remote Control is *available*, not whether the
    # direction is still the right one. So a foreign writer toggling between the two reads
    # can leave the owner asked to enable something already enabled. That window is the
    # row path's too (a rendered row fixes its direction and is never re-diffed either);
    # this key narrows it from human-paced to machine-paced rather than opening it. Noted
    # so the omission is not read as an oversight.
    directions = remote_control_directions(record, record.remote_control_state)
    opening = _REMOTE_CONTROL_KEYS[directions[0]] if len(directions) == 1 else None
    if mark_excursion:
        screen.mark_excursion()
    await screen.tui.show_detail(session_value, opening)


#: The three keys that end a session: `s` and `c` without asking (DEC-018) and `f` behind a
#: modal. Named as a set because two rules turn on it -- these are the chords a screen holding
#: typed text must not carry, and the ones that do not navigate.
#:
#: Derived from the action table rather than spelled, so a fourth lifecycle action added there
#: is refused on a text-entry screen the day it appears rather than the day someone remembers.
CHORD_STOPS = frozenset(key for key, action, *_ in SESSION_ACTION_KEYS if action in ACTION_LABELS)

#: The chords that take the owner somewhere, which is every chord that is not a stop.
CHORD_NAVIGATES = frozenset(CHORD_KEYS) - CHORD_STOPS


async def perform_chord(key: str, session_value: str, *, screen: ChoiceScreen) -> None:
    """Route one Alt chord to the same work its bare letter does on a row.

    The three destinations are the three the sessions pane has: `d` opens the detail with no
    action, `m` asks the Remote Control policy what its key means today, and everything else
    is a row action. Written as a router over the shared performers rather than as a fourth
    implementation, which is the whole point of the two functions above.
    """
    if key in CHORD_NAVIGATES - {_REMOTE_CONTROL_KEY}:
        # Every one of these navigates unconditionally -- `d` opens the detail, and `a`, `i` and
        # `r` are not in `ACTION_LABELS` so `perform_row_action` always reaches `show_detail`.
        # `m` is the exception and marks itself, because only the record says whether it moves.
        screen.mark_excursion()
    if key == _DETAIL_KEY:
        await screen.tui.show_detail(session_value)
        return
    if key == _REMOTE_CONTROL_KEY:
        # **Not marked above.** `m` is the one chord that decides what it means *after* reading
        # the record, and three of its paths return without navigating -- a store read that
        # raised, the owner having left, and the ordinary refusal of a session that is not a
        # running Claude. Marking before the read would leave the mark set on a key that went
        # nowhere, and the next genuine flow return would consume it and wrongly keep a query
        # the owner had finished with. So it marks on its own two navigating paths, and only
        # when a *chord* asked: the bare row key reaches the same function from a position that
        # never consumes the mark, and setting a one-shot flag nobody reads is a trap for the
        # next cursor-owning screen to inherit `ProjectsScreen.on_reveal`.
        await perform_row_remote_control(session_value, screen=screen, mark_excursion=True)
        return
    action = _KEY_ACTIONS.get(key)
    if action is None:
        # Unreachable through the derived bindings, which is why this returns rather than
        # raises: `chord` is a public action name and `run_action("chord('x')")` reaches here
        # from the command palette or a test, and a `KeyError` out of an action exits the app.
        return
    await perform_row_action(action, session_value, screen=screen)


class _SessionActionKeys:
    """The per-action key *behaviour* both sessions positions share.

    Methods only. The bindings that reach them are attached to the screen classes for the MRO
    reason recorded above; what lives here is the one definition of what a key does, so
    `SessionsPaneScreen` inherits it rather than holding a second copy.
    """

    #: The records behind the rows currently drawn, keyed by session id. A class-level default
    #: rather than an `__init__` assignment: this mixin is combined with `ChoiceScreen` across
    #: several screens whose `__init__`s differ, and `check_action` can run before any of them
    #: has drawn anything. Read-only, so the shared default cannot be mutated into per-instance
    #: state by accident -- `_draw_listing` rebinds the attribute rather than updating it.
    _drawn: Mapping[str, SessionRecord] = MappingProxyType({})

    def _drawn_record(self, session_value: str) -> SessionRecord | None:
        """The record this screen last *drew* for that row, if it still has one.

        A drawn record, deliberately, not a fresh read: `check_action` is synchronous and runs
        on every footer redraw, so it cannot ask the store. That is the right authority here
        anyway -- these rules describe what the row on screen offers, and DEC-007 puts the
        safety check at issue time instead, where the record is re-read and the policy
        re-checked before anything happens. A key hidden from a row that has since changed is
        a stale *advertisement*, which the next ten-second tick corrects; a key that acted on a
        stale record would be the thing DEC-007 exists to prevent, and it still cannot.
        """
        return self._drawn.get(session_value)

    def _policy_offers(self, session_value: str, action: str) -> bool:
        """Whether the drawn row's own state offers `action`. Permissive when unknown."""
        record = self._drawn_record(session_value)
        if record is None:
            # A highlighted row this screen has no record for should not silently lose its
            # keys: an absent answer is not a refusal, and the action's own chain refuses in
            # its own words if the row really is gone.
            return True
        return action in available_actions(record.state, record.orphan_provenance)

    def _remote_control_offered(self, session_value: str) -> bool:
        record = self._drawn_record(session_value)
        if record is None:
            return True
        return remote_control_available(record)

    #: This position draws its own sessions list, so a chord pressed here acts on its cursor
    #: rather than on the console's published selection. True for the pane subclass too.
    owns_session_cursor = True

    def highlighted_session(self) -> str | None:
        """The session id under the cursor, or None if the cursor is on nothing usable.

        Returns rather than raises, because a binding that raises exits the app -- the same
        reason `DashboardScreen.action_session_detail` checks its index before reading it.
        """
        # Guarded exactly as `ChoiceScreen._live_entry` is, and for its reason verbatim:
        # `query_one` raises `NoMatches` before the screen has composed, and this runs from
        # `check_action` -- which `Screen.active_bindings` calls for *every* binding in the
        # chain, `show=False` ones included, on any `bindings_updated_signal` publish. No
        # driven sequence reaches it today; a Tier-1 review reached it directly
        # (`SessionsScreen().check_action(...)` -> NoMatches). "Unreachable today" is what the
        # base class's own analog was too, and an exception out of a footer redraw is the
        # class that has already cost this app once.
        found = self.query("#choices")
        choices = found.first(OptionList) if found else None
        if choices is None:
            return None
        key = held_option_id(choices)
        # One check, not two: every sentinel row id in this package -- `_BACK`, `_CANCEL`,
        # `_EMPTY`, `NEVER_EMPTY` -- is `\x00`-prefixed, so the prefix covers all of them.
        if key is None or key.startswith("\x00"):
            return None
        return key

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Refuse the row keys where the row keys already refuse themselves.

        `ChoiceScreen.check_action`'s rule, applied to this task's additions: every entry here
        mirrors an early return that already exists in the action it governs, so the two
        cannot drift into disagreeing. `action_row_action` and `action_row_remote_control`
        both open with `highlighted_session() is None`, and that is the whole of the condition
        below.

        These bindings are `show=False`, so what this changes is not the footer -- it is
        whether Textual dispatches into a method that would do nothing. A key that is
        advertised nowhere and silently does nothing is the same complaint as a key that is
        advertised and does nothing, one step quieter.

        `False` rather than `None`, which is this file's convention throughout (`base.py`
        states it, `InspectScreen.check_action` follows it) and is identical for dispatch --
        both are falsy. The one visible difference: `False` drops the binding from
        `Screen.active_bindings`, so Textual's own keys panel, which does not filter on
        `show`, lists none of these while nothing is highlighted. That is correct -- they do
        nothing then -- but it is a discovery path, so it is named rather than left to be
        rediscovered.
        """
        if action == "row_action":
            highlighted = self.highlighted_session()
            if highlighted is None:
                return False
            # `i` is offered only where there is something to inspect, mirroring
            # `detail_entries`, which gates the Inspect row on the same capability, and
            # `show_inspect`, which returns silently without it. `p` was gated on exactly this
            # reasoning and `i` was not -- an inconsistency the stage evaluator caught.
            # Unreachable in this project's own composition (`compose_backend` always wires
            # `capture`), which is why it is a consistency fix rather than a defect repair.
            if parameters and parameters[0] == "inspect":
                return self.services.backend.capture is not None
            if parameters and parameters[0] in ACTION_LABELS:
                # Mirrors the policy for the row under the cursor, which is what every other
                # rule here does. `available_actions` returns nothing for STARTING and for an
                # ORPHANED session whose provenance is not adopted, and both are *drawn* --
                # only ENDED is filtered out of the list -- so `f` named an action nobody was
                # offering, on a row the owner is looking at. Found by this plan's close-out
                # evaluator. Attach and rename are deliberately not gated: they are not stop
                # actions and the policy says nothing about them.
                #
                # Widened from `== FORCE` to the whole of `ACTION_LABELS` when `s` and `c`
                # gained keys, and the widening is load-bearing rather than tidy. Those two
                # are offered from exactly one state each -- graceful from RUNNING, cleanup
                # from PRESERVED -- so an ungated key would be inert on most rows in the list,
                # and inert is the one thing worse than absent here: it looks identical to a
                # key that acted. Gated, `s` is simply not offered on a preserved row.
                return self._policy_offers(highlighted, str(parameters[0]))
            return True
        if action == "row_remote_control":
            highlighted = self.highlighted_session()
            if highlighted is None:
                return False
            # `remote_control_available`'s own docstring says to consult it before offering
            # the toggle and not to treat the service as a backstop for the state half. This
            # surface was the caller that did not: `m` on a codex row reached `show_detail`,
            # which is a navigation the owner did not ask for wearing a refusal's clothes.
            return self._remote_control_offered(highlighted)
        if action == "show_projects_pane":
            # Absent, not inert, on a host that wired no console. The owner cannot tell a key
            # that quietly does nothing from a surface that forgot to draw it, and this one is
            # about *where things are on screen* -- the failure would look like the console
            # being broken rather than the key being unavailable.
            return self.services.console_show_projects is not None
        return super().check_action(action, parameters)

    async def action_row_action(self, action: str) -> None:
        """Do what the key names, on the list it was pressed on — or open what it opens.

        **The split is between keys that exist to open something and keys that end a
        session.** `a`, `i` and `r` open the detail and ask it to perform the action, which is
        what they are for. `s`, `c` and `f` act here: they used to route through
        `tui.show_detail(session_value, action)` as well, which pushed a detail the owner had
        not asked for, ran the action on it, and left them there — and since a graceful stop
        that works ends the session, the detail's own re-read then rendered "That session is
        no longer available." over a single `Back` row. That screen and that row are what the
        owner reported.

        **This is a change of where the chain is entered, not a second implementation of it**,
        which is the highest-risk thing this could have been. `tui.stop` takes the screen that
        asked, re-reads the record and re-checks the policy at issue time (DEC-007's fourth
        mitigation), and calls `screen.after_command()` — whose implementation on this screen
        is `on_reveal`, a re-read of the listing in place. Nothing here checks the policy, and
        deliberately: an action the policy no longer allows is refused by the policy itself,
        in its own words, rather than by a check kept here that could drift from it.

        DEC-018 is untouched: neither `s` nor `c` gains a confirmation. Force does ask, from
        `SessionsScreen.confirm_force` — see there for why the modal is raised on this screen
        rather than on a detail pushed to hold it.

        **The session is the row under the cursor**, which is the row `session_contents` draws
        the `\u25b8` marker on -- one fact, one mark, and `_repaint_marker` is what keeps the
        second true as the first moves.
        """
        session_value = self.highlighted_session()
        if session_value is None or self.tui.busy:
            # `busy` mirrors `ChoiceScreen.on_option_list_option_selected`, which refuses a
            # pressed row while a command is in flight. `dispatch_opening` checks it again once
            # the detail exists; this is the same refusal one step earlier, so the two entry
            # paths agree rather than relying on the pump staying serialized forever.
            return
        await perform_row_action(action, session_value, screen=self)

    async def action_show_projects_pane(self) -> None:
        """Put the projects surface back in the console's left slot.

        DEC-040's exchange, run backwards. It writes no record and touches no lifecycle, so it
        needs none of the re-read-and-re-check machinery every other key here routes through --
        and DEC-041's root-key budget is untouched, because this is a screen binding inside our
        own process rather than a tmux root key. `CONSOLE_BINDINGS` is not edited.

        A failure is reported as what it is. The console degrading is not the session going
        wrong, and saying so in lifecycle terms would send the owner looking at an agent that
        is perfectly fine.
        """
        show_projects = self.services.console_show_projects
        if show_projects is None:
            return
        try:
            await show_projects()
        except Exception as error:
            _LOG.exception("the console could not show the projects surface")
            self.announce(f"The console could not show the projects surface: {error}")

    async def action_row_remote_control(self) -> None:
        """Remote Control, which is the one key that cannot name its action in advance.

        The direction is policy -- `remote_control_directions` answers Enable, Disable, or
        *both* when nobody has toggled this session and the observation is unknown. Where it
        offers one, the key performs it. Where it offers two, the key opens the detail and
        lets the owner choose: a surface that guessed would be picking a side of a question
        the policy deliberately declines to answer, on a live pane.
        """
        session_value = self.highlighted_session()
        if session_value is None or self.tui.busy:
            return
        await perform_row_remote_control(session_value, screen=self)


class SessionsScreen(_SessionActionKeys, ChoiceScreen):
    """Every managed session, including ones this process never launched.

    The one position in this surface whose answer goes stale with nobody touching it: the
    store has a second writer — the bot, and any reconcile the host runs — so a session can
    start, stop or be reconciled while the owner sits here reading. Ctrl+R has re-read it on
    demand since sub-plan 3; this screen now also re-reads itself on an interval, and stops
    doing so the moment it is not the screen on top.
    """

    draws_session_rows = True
    """This screen renders session rows, so the app's gauge cache is worth refreshing while it
    is the one showing. Read by `RemoteAgentsTui._refresh_context_windows_tick`; screens without
    it cost no provider read at all."""

    #: This position binds the bare row keys, so the Alt layer is legal here (see the flag's
    #: declaration on `ChoiceScreen`). Declared on the screen rather than on `_SessionActionKeys`
    #: for the reason `SESSION_ACTION_BINDINGS` is attached to screens: the mixin is where the
    #: *actions* live and the screen is where the *bindings* do, and this flag is about the
    #: bindings. `SessionsPaneScreen` subclasses this one and inherits both.
    carries_row_keys = True

    BINDINGS = list(SESSION_ACTION_BINDINGS)

    empty_state = "No managed sessions on this host."

    position = "SESSIONS"
    can_refresh = True
    crumb = "Sessions"

    DEFAULT_CSS = """
    SessionsScreen #choices {
        border: round $secondary; text-wrap: nowrap; text-overflow: ellipsis;
    }
    """

    #: What this position tells the owner a row does, and where an empty list sends them.
    #: Class attributes rather than literals at the call site because the console's sessions
    #: *pane* means something different by Enter and has nowhere to escape to — and a hint
    #: describing the other surface's keys is a false sentence, not a cosmetic one. The
    #: status itself is the counts (`rows.session_counts_content`); this is the muted row under
    #: it.
    listing_hint = "enter detail · esc back"
    empty_status = "No managed sessions. Press escape to go back."
    # "…to return to the project list" until the console's panes existed. This screen is
    # pushed, so escape is always real here — but the position it returns to is the
    # *pusher's* resting one, which in a feed or sessions pane process is not a project
    # list. Naming the key without naming a place is true on every surface that pushes it.

    def __init__(self) -> None:
        super().__init__()
        self._auto: Timer | None = None
        #: Whether a listing read is already in flight on this screen, keyed or scheduled.
        self._reading = False
        #: Which *visit* to this screen is current. Bumped every time the owner returns to it,
        #: and compared by `_auto_reload` across its await — see that method and `_visiting`.
        #: `showing` cannot answer this, because it is `True` again on the way back.
        self._visit = 0
        #: The content width the drawn rows were columned for, or `None` when nothing is drawn.
        #: `on_resize` compares against it so a height-only resize — and the several resizes a
        #: single layout pass emits at one width — do no work at all.
        self._laid_out_width: int | None = None
        #: Whether this screen has ever drawn a listing, which is what tells a *first fill* from
        #: a refresh. The two answer the same question — "which rows are new?" — differently and
        #: incompatibly: on a first fill every row is new and none of them arrived, so reading
        #: them as arrivals would move the target onto whichever session happens to be youngest
        #: on the host. `_drawn` cannot stand in for this, because the empty-listing branch
        #: leaves it empty on a screen that has drawn.
        self._has_drawn = False
        #: The row key the `\u25b8` marker was last *drawn* on, or `None` for no marked row.
        #:
        #: A record of what is painted, not a selection -- the selection is the cursor, and this
        #: exists only so `_repaint_marker` can tell a cursor move that needs a re-render from
        #: one that does not. Nothing reads it to decide what a key acts on, and it must not
        #: start being read that way: two answers to "which session" is exactly the ambiguity
        #: this design was reworked to remove.
        self._marked_row: str | None = None

    async def populate(self) -> None:
        self.hide_entry()
        # `keep_cursor=False` spelled out, though it is the default. This is the screen's first
        # fill: there is no cursor to keep, so row 0 is right — and saying so is what
        # `test_sessions_redraws_keep_the_cursor.py` asks of every exit. The check is not that
        # each one keeps the cursor (they do not; `redraw_after_failure` deliberately rests it
        # on nothing) but that each one *decided*. Five exits were found one at a time, each
        # measured through a wrong stop rather than caught; an omitted argument is how the
        # sixth would arrive.
        await self.reload(keep_cursor=False)
        # Started here rather than in an `on_mount` of this screen's own: the base class makes
        # `on_mount` a template method precisely so a screen cannot forget the chrome by
        # defining one, and `populate` is the hook it leaves for exactly this.
        if self._auto is None:
            self._auto = self.set_interval(_SESSIONS_AUTO_REFRESH, self._auto_reload)
        # Seed the gauges now rather than at the first sixty-second tick. The dashboard already
        # does this in its own `populate`; without it here, the console's sessions pane -- which
        # mounts this screen as a process of its own and has no dashboard to seed it -- drew
        # rows with no gauge for up to seventy seconds after launch.
        await self._seed_context_gauges()

    async def _seed_context_gauges(self) -> None:
        """Fill the app's gauge cache once, then redraw, without the list-open pass.

        **Guarded exactly as `_auto_reload` is, and it was not.** This is the fourth
        unserialised fill of this listing — the interval, `on_reveal`, a resize, and this —
        and it has the same shape as the interval: read records, await something slow, then
        draw the records read *before* that await. It carried neither of the two guards that
        method documents, on the one path that runs at mount, where the per-session provider
        sweep is at its slowest.

        The failure that leaves is the one `on_screen_resume` already describes in the other
        direction — "a session that ended during the detour is put back on screen by the stale
        listing". Where the sweep outruns the ten-second interval: the tick reads a list without
        the session that just ended, draws it, and correctly clears the cursor; then this
        resumes and redraws its stale list with `keep_cursor=True`, putting the ended session
        back under the cursor `s` acts on with no confirmation.

        `_reading` stops a tick running underneath, so the two cannot interleave and leave the
        scheduler to decide which lands last. `_visit`, captured before the await and compared
        after, drops a listing belonging to a visit the owner has since left.

        **And the fill counter, which is the guard the other two do not add up to.** An earlier
        version of this docstring closed with "what it can no longer do is overwrite a *newer*
        listing" while holding only the first two, and that was false: `_visit` moves on
        navigation alone, `refresh_contents` (Ctrl+R) and `after_command` bump nothing, and
        `reload` deliberately does not stand down for `_reading` because a keyed re-read is the
        owner asking again. So a Ctrl+R landing mid-sweep drew fresh records and this then
        redrew its stale ones over the top. `_resting_generation` is taken by every
        `show_choices` exit, so comparing it catches a fill by whatever route it arrived.

        The residual, stated correctly this time: this still draws records as old as its own
        sweep when nothing else has drawn — it redraws what it read, which is the point of a
        seed, and the next tick corrects it. What it cannot do is land on top of a newer
        listing.

        One interlock it does not repair: `_reading` is a flag rather than a counter, so a
        `reload` finishing mid-sweep clears it and a tick can then start beside this. The fill
        counter makes that harmless here — whichever draws second, the other stands down — but
        `_auto_reload`'s "never over work in flight" is weaker than it reads, and the flag is
        shared, so widening it is not this method's to do.
        """
        if self.tui.services.backend.usage is None:
            return
        visiting = self._visit
        filled = self._resting_generation
        self._reading = True
        try:
            records = await self.tui.read_sessions()
            await self.tui.refresh_context_windows(records)
        except Exception:
            _LOG.debug("the session context gauges could not be seeded", exc_info=True)
            return
        finally:
            self._reading = False
        if visiting != self._visit or filled != self._resting_generation:
            return
        self._draw_listing(records, keep_cursor=True)

    def on_screen_suspend(self) -> None:
        """Stop polling the store for a screen the owner is no longer looking at.

        Not merely wasted work. `load_sessions` refreshes readiness before it lists, which
        talks to tmux — so an unpaused interval would keep a background conversation with the
        runtime going underneath every detail, confirmation and inspect screen pushed on top
        of this one, for as long as the owner stayed there.

        `ScreenSuspend` here and `on_reveal` for the re-read are not two spellings of one
        idea, and the base class documents why the re-read cannot use these: `go_back` awaits
        `on_reveal`, while a resume handler runs on the pump after the pop returns, outside
        the guard a stop may still hold. Pausing a timer needs no such ordering, so the
        framework hook is the right one for this half.
        """
        if self._auto is not None:
            self._auto.pause()

    def on_screen_resume(self) -> None:
        """Resume polling, and retire any read still in flight from the previous visit.

        The bump is the fix for a stale repaint. `on_screen_suspend` pauses the *timer*, which
        stops new reads being scheduled, but it cannot recall the one already awaiting
        `load_sessions` when the owner pushed a detail on top. That read resolves whenever the
        store answers, which may be after the owner has come back — and by then `showing` is
        `True` again,
        because it asks `app.screen is self` and this screen is once more what the owner is
        looking at. So the guard that exists to catch exactly this cannot see it.

        Recorded outcome without the bump: a session that ended during the detour is put back
        on screen by the stale listing, offering `Stop` against a pane that no longer exists.

        Deliberately a per-visit counter rather than a redefinition of `showing`. `showing`
        answers "is this screen what the owner is looking at", which is the right question for
        its eleven other callers and is a *different* question from "is this still the same
        visit the read was issued during". Conflating them would fix this and quietly change
        every render guard in the package.
        """
        self._visit += 1
        if self._auto is not None:
            self._auto.resume()

    async def _auto_reload(self) -> None:
        """The interval's re-read: quiet, cursor-preserving, and never over work in flight.

        Every one of those three is a defect this would otherwise have introduced, and the
        loud version of this method is worse than no auto-refresh at all:

        - **Quiet.** `reload` wraps its read in `awaiting(...)`, which is right when the owner
          pressed a key and is waiting for an answer. On a timer it would flash "Reading the
          managed sessions…" over the status line every interval forever.
        - **Cursor-preserving.** `show_choices` rests the cursor on row 0 by default. An
          unqualified refill would walk the owner's selection back to the top of the list on
          every tick, and on the tick they pressed enter it would open a different session's
          detail than the one they were looking at.
        - **Never over work in flight.** Two different guards, because the first version of
          this docstring named only one and overclaimed it. `self.tui.busy` covers a mutating
          command — and **`busy` is now the whole of that half rather than the belt to a
          braces.** Those commands used to be issued only from the *detail* screen, which
          suspends this timer by being pushed on top; since `s`, `c` and `f` moved onto this
          list they are issued from **here**, with no detail pushed and the timer running. The
          suspension no longer covers them, which makes `busy` load-bearing where it used to be
          redundant, and makes the `_visit` bump on the re-read after a command the thing that
          discards a tick which was already in flight.
          What `busy` does not cover is this screen's own reads: Ctrl+R and `on_reveal` call
          `reload`, which holds no guard at all, so a tick landing mid-refresh used to start a
          second concurrent `load_sessions` — doubling the readiness probe on a host already
          slow enough to make it overlap, with whichever draw finished last winning and
          silently discarding the manual refresh's cursor reset. `_reading` closes that.

        A failed background read is logged and swallowed rather than announced. The owner did
        not ask for this read, and a store that is briefly unreadable would otherwise raise a
        toast every interval; Ctrl+R still reports the failure loudly, because that one *was*
        asked for.
        """
        if not self.showing or self.tui.busy or self._reading:
            return
        # Captured *before* the await, compared after: the read belongs to the visit it was
        # issued during, and a visit the owner has since left and returned to is a different
        # one. `showing` is checked above and again inside `_draw_listing`, and
        # neither can answer this — see `on_screen_resume`.
        visiting = self._visit
        self._reading = True
        try:
            records = await self.tui.load_sessions()
        except Exception:
            _LOG.warning("the background session re-read failed", exc_info=True)
            return
        finally:
            self._reading = False
        if visiting != self._visit:
            return
        self._draw_listing(records, keep_cursor=True)

    async def on_reveal(self) -> None:
        """Re-read on the way back from a detail, as the hand-rolled chain did.

        **The `_visit` bump belongs here and not only in `on_screen_resume`**, and a Tier-1
        review caught why. `on_screen_resume` is delivered as a `ScreenResume` *message*, so
        it runs on this screen's own message-pump task whenever that task next drains — while
        `go_back` calls `pop_screen()` and then awaits this method directly, on the app's
        task. `go_back`'s own docstring already says so: "Textual's own `ScreenResume` would
        run after the pop returned, which is outside that guard."

        So bumping only there left the answer dependent on which task the scheduler resumes
        first once a stale read's store call returns: this screen's pump, delivering
        `ScreenResume`, or the read itself. Measured, the pump happens to win — the ordering
        is `go_back returned -> on_screen_resume -> read landed`, and a test cannot pin it
        either way precisely because nothing promises it. Bumping here removes the dependency
        rather than winning the race: this is awaited synchronously at the moment the screen
        is revealed, so the counter has already moved whoever runs next.

        Both bumps are kept — the counter only has to *change*, so bumping twice on a back
        path is harmless, and `on_screen_resume` still covers the paths that do not come
        through `go_back`.

        **`keep_cursor=True`, and this is the one place that closes a whole class.** Every
        redraw of this listing that resets the cursor to row 0 is a hazard, because `s` and
        `c` act on the row under the cursor without asking (DEC-018) and the import-time guard
        above says so in terms: a key must not be able to act on a row the owner did not put
        the cursor on. Five exits redraw this list, and they were fixed one at a time as each
        was found — `after_command` after a stop, `redraw_after_failure` after one that
        raised, `confirm_force`'s abort, `ChoiceScreen.refuse`, and `refresh_contents` on
        Ctrl+R — of which the middle two both come through *here*. Measured before this: three
        RUNNING sessions, cursor on row 2, `f` then escape, then `s` — one graceful stop issued
        against **row 0**, a session the owner never selected.

        **The fifth is the one this funnel does not catch, and it is worth naming as such.**
        `refresh_contents` does not navigate, so it never reaches `on_reveal`; it carries its
        own `keep_cursor=True` and says why there. An earlier version of this paragraph said
        four exits and called the funnel the place that "closes a whole class" — the funnel
        closes every exit that arrives by *navigation*, which is a smaller class than the one
        the hazard belongs to. `test_sessions_redraws_keep_the_cursor.py` is what makes a sixth
        arrival visible without waiting to be measured through a wrong stop.

        So the fix belongs at the funnel rather than at each mouth. `keep_cursor` restores by
        row *key* and rests on **nothing** when that key has gone, which is the mechanism
        DEC-052 built for the ten-second refresh and the one `_CLEARS_VANISHED_CURSOR`
        describes. It is also simply better on the path this method was written for: coming
        back from a detail, the owner lands on the row they opened rather than at the top.
        """
        self._visit += 1
        await self.reload(keep_cursor=True)

    async def refresh_contents(self) -> None:
        """Re-run readiness and the listing, which is what Refresh means on this screen.

        The same work as `on_reveal`, and this is the position where the key earns its place:
        the store has a second writer, so this list can go stale with the owner sitting on it
        and no navigation to trigger a re-read. Until this task, Ctrl+R here re-read the
        project catalogue and unwound to the project picker.

        **`keep_cursor=True`, and this is the fifth exit** in the class `on_reveal` enumerates.
        Moving that fix to the `on_reveal` funnel closed four mouths at once, and this one is
        not among them for the reason that makes it easy to miss: Refresh does not navigate, so
        it never reaches `on_reveal` at all. Reloading with the default rested the cursor on
        row 0, and `Ctrl+R` then `s` issued a graceful stop against a session the owner never
        selected — the same measured shape as the `f`-escape-`s` sequence that funnel was built
        for.

        Re-reading and re-choosing are different acts. The owner pressed a key meaning "show me
        what the store holds now"; nothing in that asks for a different row, and on this list
        the row is the handle on two unconfirmed stops (DEC-052, DEC-062).
        """
        await self.reload(keep_cursor=True)

    async def reload(self, *, rest_on_nothing: bool = False, keep_cursor: bool = False) -> None:
        """Refresh readiness, then list what the shared store actually holds — on request.

        `rest_on_nothing` is asked for by `redraw_after_failure` alone: a command that raised
        leaves the row it failed on under the cursor, and `s`/`c` carry no confirmation.

        Sets `_reading` for its duration so the interval stands down rather than issuing a
        second concurrent probe underneath a refresh the owner actually asked for. It does
        not *check* the flag: a keyed re-read is the owner asking again, and refusing that
        because a background tick happens to be in flight would be the surface ignoring them.

        Readiness is refreshed first for the same reason the bot does it: a launch that
        failed here may have become ready since, and listing a stale FAILED would send the
        owner to fix something that already works.
        """
        self._reading = True
        try:
            async with self.awaiting("Reading the managed sessions…"):
                records = await self.tui.load_sessions()
        except Exception as error:
            self.tui.report_store_failure(error, self)
            return
        finally:
            self._reading = False
        self._draw_listing(records, rest_on_nothing=rest_on_nothing, keep_cursor=keep_cursor)

    def draw_failure_rows(self, entries: tuple[tuple[str, str | Content], ...]) -> None:
        """Fill after a failed read, and publish the cursor that fill leaves behind.

        The same obligation every other fill of this listing carries, arriving through the one
        route that used to escape it. On a console pane there is no screen to go back to, so
        this draws nothing, and `show_choices` substitutes the *disabled* empty-state row —
        which Textual highlights without posting a message, so the move handler never fires.
        The cursor ends on nothing while the option went on naming the session before it.
        """
        super().draw_failure_rows(entries)
        self._publish_selection(self.highlighted_session())

    def _publish_selection(self, session_value: str | None) -> None:
        """Publish which session this position has selected. Nothing, on this position.

        **`SessionsScreen` deliberately does not publish, and the reason is the same one that
        gates `p` to the pane** (`SessionsPaneScreen.BINDINGS`). Hosting is decided by the tmux
        socket name, so a plain `remote-agents tui` started from any shell on the console's
        server is classified CONSOLE and gets `console_publish_selection` wired — and a cursor
        moving in that unrelated process would redirect the chords of the owner's *real*
        console, from a window that is not one of its three panes at all.

        A hook rather than a capability check, because "am I one of the console's panes" is a
        question about which screen this is, and the screen is the thing that knows. Checking
        the wiring instead would answer "is a console reachable", which is true in both cases.
        """
        return None

    def _draw_listing(
        self,
        records: tuple[SessionRecord, ...],
        *,
        keep_cursor: bool = False,
        rest_on_nothing: bool = False,
    ) -> None:
        """Draw the listing, then publish the cursor it actually left behind.

        **A funnel, because publishing per branch got it wrong twice.** `_draw_rows` has four
        exits and three of them can leave no usable cursor: an empty listing (whose disabled
        placeholder Textual highlights *without* posting a message, so the move handler never
        fires), `rest_on_nothing` after a stop that raised, and the vanished-row branch. Only
        the last was publishing. The other two left the option naming the session that had just
        ended, or — worse — the row a stop had just failed on, which is demonstrably still live.

        `redraw_after_failure` is the one that matters most: it rests the cursor on nothing
        precisely because `s` and `c` carry no confirmation and a repeated keypress would
        re-issue a stop nobody chose (DEC-018, DEC-062). That mitigation is local to this pane,
        and leaving the option set exported the hazard to every pane that reads it.

        So the publication is derived from the *result* rather than asserted by each branch: one
        call, after the draw, reading the cursor the draw produced. A fifth exit cannot forget
        it, which is the same repair the `on_reveal` funnel made for `keep_cursor` and the same
        lesson the Stage 1 gate paid for — a set enumerated by hand is a set with a member
        missing.
        """
        self._draw_rows(records, keep_cursor=keep_cursor, rest_on_nothing=rest_on_nothing)
        self._publish_selection(self.highlighted_session())

    def _newest_arrival(
        self, records: tuple[SessionRecord, ...], previously_drawn: frozenset[str]
    ) -> str | None:
        """The most recently created session this draw has that the last one did not.

        Only called where the screen has drawn before, because on a first fill every row is an
        arrival and none of them arrived — see `_has_drawn`.

        **By `created_at` rather than by list position**, because more than one session can
        appear between two ten-second ticks and the listing's order is the store's, not the
        clock's. Where several arrive at once the youngest wins, which is the one the owner's
        last act produced.
        """
        arrivals = [record for record in records if str(record.session_id) not in previously_drawn]
        if not arrivals:
            return None
        return str(max(arrivals, key=lambda record: record.created_at).session_id)

    def _repaint_marker(self) -> None:
        """Move the `\u25b8` onto the row the cursor is now on, in place.

        **The marker is a *rendering* of the cursor, never a second selection.** What `s` ends
        is `highlighted_session()` and nothing else; this method's whole job is to keep the
        drawn mark agreeing with that as the cursor moves, so the row a key will act on is
        legible from a pane that is not this one.

        Memoised on the row it last drew, which is what makes it cheap enough to run from a
        highlight handler. `show_choices` assigns `highlighted` during a fill, and that posts a
        highlight message of its own -- so without the memo every draw would be followed by a
        redundant re-render of every row it had just rendered.

        In place (`_replace_prompts`) rather than through a refill, and that is the same
        requirement `on_resize` has for the same reason: a refill would bump
        `_resting_generation`, schedule a fresh `_rest_cursor`, and re-arm the window an arrow
        press can land in -- from a handler that runs *on* an arrow press.
        """
        key = self.highlighted_session()
        if key == self._marked_row:
            return
        self._marked_row = key
        if not (self.showing and self._drawn):
            return
        found = self.query("#choices")
        choices = found.first(OptionList) if found else None
        if choices is None:
            return
        self._replace_prompts(choices, self._laid_out_width)

    async def on_option_list_option_highlighted(self, event: object) -> None:
        """The cursor moved, so the mark moves with it and the console is told.

        Both halves are the same fact stated twice, once on screen and once on the tmux option
        the other panes read: this row is what a stop key ends. `SessionsScreen`'s
        `_publish_selection` is a no-op, so off the console this is the repaint alone.
        """
        self._repaint_marker()
        self._publish_selection(self.highlighted_session())

    def _replace_prompts(self, choices: OptionList, width: int | None) -> None:
        """Re-render every drawn row at `width` and swap the prompts the list already holds.

        Shared by the resize re-lay and the marker repaint, which want the same thing for
        different reasons: neither re-decides *which* rows there are, so neither may clear the
        list. `OptionList.replace_option_prompt` mutates the `Option` in place, so the objects
        survive — which is the predicate DEC-069 drops a queued selection on, and the reason a
        row the owner aimed at across one of these is still the row that is there.

        Rows the list does not hold are skipped rather than raising: `report_store_failure`
        leaves a lone `Back` row on a screen whose `_drawn` still names sessions, and neither
        caller may take the screen down with `OptionDoesNotExist` over a row it cannot paint.
        """
        records = tuple(self._drawn.values())
        keys = [str(record.session_id) for record in records]
        marked = self._marked_row
        active = keys.index(marked) if marked in keys else None
        parts = [
            session_row_parts(record, self.tui.context_window_for(record.session_id))
            for record in records
        ]
        held = {option.id for option in choices.options}
        contents = session_contents(parts, width, active, marks_active=True)
        for key, content in zip(keys, contents, strict=True):
            if key in held:
                choices.replace_option_prompt(key, content)

    def _resting_row(
        self,
        keys: list[str],
        choices: OptionList,
        *,
        arrived: str | None,
        rest_on_nothing: bool,
        keep_cursor: bool,
    ) -> int | None:
        """Which row this draw rests the cursor on. Four answers, in order, `None` for nothing.

        **Hoisted out of the render branches rather than invented**, and the ordering is the
        safety argument it always was. What is new is only that the answer is needed *before*
        the rows are built, because the resting row is drawn with the `\u25b8` marker on it.

        1. **Nothing**, after a stop that raised (`rest_on_nothing`). The session the command
           failed on is demonstrably still live -- the failure is why it is still there -- and
           `s`/`c` carry no confirmation, so a repeated keypress must find nothing rather than
           re-issue a stop nobody chose (DEC-018, DEC-062). It outranks an arrival: a failed
           stop is not a moment to hand the keys a fresh subject.
        2. **A session that has just started.** The owner named it by starting it, so the cursor
           goes to it and the keys go with it -- this is the one exit that moves the cursor on
           the owner's behalf, and `_newest_arrival` carries the rest of that argument.
        3. **Row 0**, on a fill that has no cursor to keep. First fills only; every caller that
           holds a cursor passes `keep_cursor=True`.
        4. **The held row, restored by key -- or nothing when that key has gone.** This is
           `_CLEARS_VANISHED_CURSOR`, which the import-time invariant above reads and which is
           the whole of the argument for binding an unconfirmed `s` at all. Restoring by *key*
           and not by index, because a session ending between two ticks shortens the list above
           the cursor and index `n` then names a different session. Falling back to row 0 --
           which this used to do -- is a live agent selected by a ten-second timer nobody
           pressed; DEC-052 named that as its central hazard and listed clearing the highlight
           as its rejected alternative 3, calling it "a genuine improvement".

        DEC-007 is honoured rather than traded away by answer 4. Its rule is that a *resting*
        cursor is never on something that mutates, and no cursor at all satisfies that strictly:
        every row key returns early on `highlighted_session() is None`, and Enter reaches no
        row. One arrow press brings the cursor back, deliberately -- the owner choosing a row
        again is the deliberate act the vanished one can no longer stand in for.
        """
        if rest_on_nothing:
            return None
        if arrived is not None:
            return keys.index(arrived)
        if not keep_cursor:
            return 0
        current = held_option_id(choices)
        return keys.index(current) if current in keys else None

    def _draw_rows(
        self,
        records: tuple[SessionRecord, ...],
        *,
        keep_cursor: bool = False,
        rest_on_nothing: bool = False,
    ) -> None:
        """Draw a listing, deciding where the cursor rests **before** the rows are rendered.

        One decision, taken in one place, and the ordering is what this method had to change
        for. The cursor row is drawn differently -- it carries the `\u25b8` marker and a yellow
        identity, which is how the owner sees from another pane which session an unconfirmed
        `s` will end -- so the rows cannot be built until the resting row is known. This used to
        pick the highlight in four branches *after* rendering; the four answers are unchanged
        and now live in `_resting_row`, above the render rather than below it.

        **Guarded on `showing`, and the guard is load-bearing rather than defensive.** Every
        other render entry point in this class holds one, and this one reached around it: the
        `keep_cursor` branch dereferences `#choices` directly, before any call that would have
        checked. `_auto_reload` checks `showing` *before* awaiting the store, so a screen
        popped during a slow read arrived here with its widgets already removed and
        `query_one` raised `NoMatches` — inside a `Timer` callback, where `Timer._tick` hands
        any exception to `App._handle_exception`, whose own docstring reads "Always results in
        the app exiting". A background refresh nobody asked for could take the surface down,
        and the window is widest exactly when the host is slow to answer a readiness probe,
        which is when this feature is worth having. Reproduced before the fix:
        `NoMatches: No nodes match '#choices' on SessionsScreen()`.

        Named `_draw_listing` and not `_render`, which is what it was called for exactly one
        test run: `Widget._render` exists, and overriding it with a different signature broke
        every screen that tried to paint itself — `TypeError: _render() missing 1 required
        positional argument`. A private name on a framework subclass is only private from
        other modules, not from the base class.

        Shared by the keyed re-reads and the interval, so the two cannot drift into rendering
        the same store differently — which is the whole reason the interval does not simply
        call `reload`.
        """
        if not self.showing:
            return
        # Captured *before* the rebuild below, because the whole of "which sessions are new" is
        # the difference between the two. A first fill is excluded by `_has_drawn` rather than
        # by this being empty: they look identical here and mean opposite things.
        previously_drawn = frozenset(self._drawn)
        drawn_before = self._has_drawn
        self._has_drawn = True
        # Rebuilt on **every** draw, this branch included, so `_drawn` cannot outlive the rows
        # it describes -- see `_drawn_record`. Assigned before the empty-list return rather
        # than after it: a last session ending leaves that branch drawing an empty list, and a
        # version of this that rebuilt only on the non-empty path left the defunct records
        # behind. Nothing could reach them today, because `show_choices(())` draws a disabled
        # sentinel row and `highlighted_session()` filters every sentinel id -- but that is an
        # unrelated guard in another method holding up an invariant this one asserts, which is
        # the shape that stops being true the first time someone adds a second reader. Found by
        # this change's Tier-1 review.
        self._drawn = {str(record.session_id): record for record in records}
        choices = self.query_one("#choices", OptionList)
        choices.border_title = sessions_title(len(records))
        if not records:
            self.show_choices(())
            self.set_status(self.empty_status, hint="")
            # Nothing is columned, so nothing is laid out at any width. Cleared rather than
            # left behind: a resize arriving while the list is empty must not be able to match
            # a width recorded for rows that are gone, and the next fill records its own.
            self._laid_out_width = None
            # Nothing is drawn, so nothing is marked. The memo is what `_repaint_marker`
            # compares against, and leaving it naming a row that is gone would make the next
            # cursor move onto a *rebuilt* list compare equal and skip its repaint.
            self._marked_row = None
            return
        # The counts are the facts; the keys are the hint. Both from the tuple the rows are drawn
        # from -- one read (the rule `_sessions_reply` states for its own header).
        self.set_status(session_counts_content(records), hint=self.listing_hint)
        parts = [
            session_row_parts(record, self.tui.context_window_for(record.session_id))
            for record in records
        ]
        width = choices.content_size.width or None
        # Recorded so `on_resize` can tell a width change from the several same-width resizes a
        # single layout pass emits. Set on every fill rather than only in `on_resize`, because
        # a fill is also a lay-out and leaving it stale would make the next genuine width
        # change look like a repeat.
        self._laid_out_width = width
        keys = [str(record.session_id) for record in records]
        arrived = self._newest_arrival(records, previously_drawn) if drawn_before else None
        resting = self._resting_row(
            keys,
            choices,
            arrived=arrived,
            rest_on_nothing=rest_on_nothing,
            keep_cursor=keep_cursor,
        )
        # The marker goes on the row this draw is about to rest the cursor on, and the memo
        # records it so the highlight message `show_choices` posts finds nothing to repaint.
        self._marked_row = None if resting is None else keys[resting]
        contents = session_contents(parts, width, resting, marks_active=True)
        rows = tuple(zip(keys, contents, strict=True))
        if rest_on_nothing:
            # Through `show_choices`'s own `highlight=None` rather than assigning `highlighted`
            # afterwards — which does not work: that call schedules `_rest_cursor` with
            # `call_after_refresh`, so a later assignment is overwritten by the deferred rest.
            # Measured, when the first version of `redraw_after_failure` did exactly that and
            # the cursor came back on row 0.
            self.show_choices(rows, highlight=None)
            return
        if not keep_cursor and arrived is None:
            # A first fill, whose answer `_resting_row` already computed as row 0. Spelled out
            # rather than defaulted: `test_every_listing_fill_states_its_highlight` asks every
            # fill of this listing to state one, and an omitted argument is how a sixth redraw
            # exit arrives.
            self.show_choices(rows, highlight=resting)
            return
        # Both remaining answers — the arrival's row, and the held row restored by key — keep
        # the keyboard where it is, because either can be reached by a background tick.
        self.show_choices(rows, focus=choices.has_focus, highlight=resting)

    def on_resize(self, event: events.Resize) -> None:
        """Lay the columns out again for the new width, from the rows already drawn.

        **Neither a refill nor a cursor event.** This used to rerun the whole of
        `_draw_listing`, which clears the list and adds it back — so every resize bumped
        `_resting_generation`, scheduled a fresh `_rest_cursor`, and re-armed the window an
        arrow press can land in between a fill and its deferred placement. In the console that
        is every DEC-040 exchange and every drag, which made a cursor hazard out of dragging a
        pane border. A resize changes how wide the rows are drawn and nothing else; it has no
        business deciding where the cursor is.

        Two narrowings, and the first is not an optimisation — but it is not what an earlier
        version of this paragraph claimed either. It said "a single layout pass emits several
        `Resize` events at one width", which is true only of a height-only resize. **Measured**
        on a 100→60 terminal: this handler is dispatched *before* `Screen._on_resize`, because
        `MessagePump._get_dispatch_methods` walks the MRO and reaches this override first — and
        `Screen._on_resize` is where the children are actually re-laid. So the first delivery
        carries the new *screen* width beside the old *child* width (60 and 96), and only a
        second, post-layout delivery reports the child at 56.

        Comparing against `_laid_out_width` therefore used to swallow that first event by
        coincidence — the stale read happened to equal the memo — and correctness rested on a
        second `Resize` that nothing in Textual promises. The re-lay is now deferred through
        `call_after_refresh`, which runs after the layout this event triggers, so the width is
        read once and read correctly. The memo then means what it says: skip when the width the
        rows were columned for has not changed.

        The second replaces each row's prompt in place instead of clearing and refilling.
        `OptionList._replace_option_prompt` mutates the `Option` the list already holds, so the
        objects survive — which is the predicate **DEC-069** drops a queued selection on. Under
        the old refill a selection queued across a resize was dropped as stale; it is now
        honoured, because the row the owner aimed at is the row that is still there. That is
        the intended reading and it narrows DEC-069's reach, so it is stated rather than left
        to be discovered: the shapes that still replace objects are the ones that genuinely
        re-decide the rows — every `show_choices` fill, and so every `reload`, tick and
        navigation. `test_a_queued_burst_reaches_the_screen_twice` pins DEC-069's own
        discriminator and is unaffected, because it re-renders nothing.

        Rows the list does not hold are skipped rather than raising. `_drawn` and the drawn
        options agree on every path that fills from records, but `report_store_failure` leaves
        a lone `Back` row on a screen whose `_drawn` still names sessions, and a resize landing
        there must re-column what it can and leave the rest alone rather than take the screen
        down with `OptionDoesNotExist`.
        """
        if not (self.showing and self._drawn):
            return
        self.call_after_refresh(self._relay_columns)

    def _relay_columns(self) -> None:
        """Re-column the drawn rows for the width the widget actually has, post-layout.

        Split from `on_resize` so the width is read after the layout that event triggers
        rather than before it — see that docstring for the measurement. Re-checks its own
        preconditions because it runs a refresh later, by which time the screen may have been
        left or the listing refilled.
        """
        if not (self.showing and self._drawn):
            return
        choices = self.query_one("#choices", OptionList)
        width = choices.content_size.width or None
        if width == self._laid_out_width:
            return
        self._laid_out_width = width
        self._replace_prompts(choices, width)

    async def choose(self, key: str) -> None:
        if key == _BACK:
            # BL-020's other instance. `report_store_failure` renders a lone `_BACK` row onto
            # the screen whose read failed; this method used to route every key to
            # `show_detail`, so choosing it asked the store for a session called `\x00back`
            # and answered "That session is no longer available" — the wrong cause, on the
            # path that runs when something is already broken. The shared handler now catches
            # this before `choose` is reached; the branch is here because `choose` is also
            # called directly, and because a screen should be able to answer for its own rows.
            await self.tui.go_back()
            return
        await self.tui.show_detail(key)

    async def redraw_after_failure(self) -> None:
        """Re-read the listing and rest the cursor on nothing.

        The base class collapses to a lone `Back` row, which is right for a screen describing
        one record and destroys this one: a single stop that raised would hide every other
        session on the host, which is the reported defect with a larger blast radius.

        Both halves matter. The **re-read** is what keeps the other rows — they are still
        accurate, and the one that failed may have moved. **Resting the cursor on nothing** is
        what the base class was buying with its lone `Back`: after a failure the row that
        failed is still under the cursor and `s`/`c` carry no confirmation (DEC-018), so a
        repeated keypress would re-issue a stop nobody deliberately chose. The same answer
        `_draw_listing` already gives a vanished row (`_CLEARS_VANISHED_CURSOR`) and the same
        one `ProfilesScreen` gives a failed launch — a list whose keys act on a live session
        may not have its cursor moved by anything but the owner.

        `reload` rather than `on_reveal` so this is one read rather than two, with the `_visit`
        bump `on_reveal` owns kept.

        **That bump closes a real race, and an earlier version of this paragraph said it did
        not.** It claimed `tui.stop` holds `busy` end to end and `_auto_reload` returns while
        `busy`, so nothing could be in flight to invalidate — which confuses *scheduling* a
        tick with one already suspended inside `load_sessions`. `busy` refuses ticks that have
        not started; a read that was already awaiting the store resumes and draws whatever it
        fetched. Measured by the Stage 2 second review pass: gate a tick inside the store read,
        stop a session, let the read answer, and the stopped session is drawn back onto the
        list as RUNNING, offering `s` and `f` against a pane that no longer exists. The bump is
        what discards it — the same mechanism, and the same defect, `on_screen_resume` records
        for the detour through a detail.
        """
        self._visit += 1
        await self.reload(rest_on_nothing=True)
        # `reload` writes the listing's own status, which is the right one here: the rows are
        # re-read and current, so what this position has to say is what it always says. The
        # base class's "go back and open the session again" would be an instruction to leave a
        # position the owner is already on, about a state now on screen — and on the console
        # pane it would blank that pane's keymap line until the next tick.
        if self._drawn:
            self.set_status(session_counts_content(tuple(self._drawn.values())))


class SessionsPaneScreen(SessionsScreen):
    """The console's right-top pane: the same list, where Enter opens instead of describing.

    The one pane that stays on screen while an agent occupies the left slot, so it is the
    only place the owner can reach back from — which is why Enter here means *exchange this
    agent into the left pane* rather than *tell me about it*. The detail, where every stop,
    inspect, rename and Remote Control affordance lives, moves to `d`, so DEC-007's full
    action set is one key away rather than gone.

    That pairing is not new: the combined dashboard's sessions region has meant exactly this
    since it gained one. What changes is that the list is now a screen of its own, in a
    process of its own, and inherits every one of `SessionsScreen`'s stale-read guards
    unchanged.

    The resting cursor stays on a non-mutating row (DEC-007) and this pane satisfies that by
    what Enter *is*: an exchange writes no record and touches no lifecycle (DEC-040).

    **Every mutating action used to be behind `d`, and three of them no longer are.** `s`, `c`
    and `f` act on this list directly — that is ask 6, and `action_row_action` above carries
    the full account. What still holds is the sentence that matters for this pane's own safety
    argument: *Enter* mutates nothing here. What changed is that a bare `s` or `c` now ends a
    session without the detail in between, which is safe only because `_resting_row` rests a
    vanished cursor on nothing and `after_command` keeps the cursor on the row the owner acted
    on. Both are inherited from `SessionsScreen` rather than restated here.
    """

    #: Its own name, not `SESSIONS`. It shares the sessions screen's body and inherits its
    #: machinery, but its status now says something different — because Enter here means
    #: something different — so a single committed baseline could only cover one of the two
    #: renders while appearing to cover both.
    position = "SESSIONS_PANE"

    #: Enter opens rather than describes here, and this pane *is* its process's resting
    #: position — so there is no project list to escape to and escape at rest is inert.
    #: Inherited unchanged, both sentences named the other surface's keys. Found by driving
    #: the real pane at the Stage 1 gate, which is the only place a false status shows.
    listing_hint = "enter open · d detail · p projects · F12 from inside an agent"
    empty_status = (
        "No managed sessions on this host. Launching one starts it here. "
        "p returns the projects pane (or F12 from inside an agent)."
    )
    read_failure_route = "Ctrl+R re-reads this screen."

    BINDINGS = [
        # Hidden from the footer for the reason the dashboard's copy is: the bar is shared
        # with every inherited binding, and the key only means something while a row is
        # highlighted. The status line says so where it is true.
        Binding("d", "session_detail", "Session detail", show=False),
        # Repeated rather than inherited, because Textual merges `BINDINGS` across the MRO and
        # a subclass that declares its own does *not* lose its parent's -- but this file has
        # been bitten once already by assuming how that merge works, so the set this position
        # offers is written where a reader can see it whole.
        *SESSION_ACTION_BINDINGS,
        # `p` is offered **here and not on `SessionsScreen`**, and the difference is not
        # cosmetic. Hosting is decided by the tmux socket name -- `bootstrap.local_context`
        # says so in terms: "hosting is decided by the socket name, which is true of every
        # pane on this server". So a plain `remote-agents tui` started from any shell on the
        # console's server is classified CONSOLE and gets `console_show_projects` wired, and a
        # `p` on the full sessions position would then rearrange the owner's real console from
        # a process that is not one of its three managed panes at all.
        #
        # This pane *is* one of them. Found by the Stage 4 Tier-2 pass, which noted that the
        # other four console capabilities are either never invoked from `SessionsScreen` or
        # (`open_in_console`) invoked only from this subclass -- so `p` would have been the
        # first to break that pattern.
        _SHOW_PROJECTS_BINDING,
    ]

    async def choose(self, key: str) -> None:
        """Enter exchanges the chosen agent into the left slot; Back still goes back.

        Routed through the app's one open seam, so hosting decides what opening means — the
        exchange under the console, the exec handoff in a bare terminal — and this screen
        never has to know which it got.
        """
        if key == _BACK:
            await self.tui.go_back()
            return
        await self.tui._open_or_leave(key)

    def __init__(self) -> None:
        super().__init__()
        #: The selection waiting to be written, and whether one is waiting. A *slot*, not a
        #: queue: intermediate rows an arrow swept through are not worth a tmux round trip
        #: each, and the only value that has to reach the option is the last one.
        self._pending_selection: SessionId | None = None
        self._selection_pending = False
        #: Serializes the writes. See `_write_selection` for why one is not enough on its own.
        self._selection_lock = asyncio.Lock()
        #: The last value successfully written, so an unchanged cursor costs nothing. Measured
        #: before this: one quiet ten-second tick published the same id **three** times — the
        #: funnel, then `show_choices`'s synchronous highlight through the move handler, then
        #: `_rest_cursor`'s deferred re-assert through it again — so an idle console spent
        #: eighteen `fork`/`exec`s a minute restating a fact that had not changed. Only updated
        #: on success, so a failed write is retried by the next identical value rather than
        #: swallowed.
        #:
        #: **Accepted cost: the memo is this process's record of what *it* wrote, not a reading
        #: of the option.** Where two `SessionsPaneScreen` processes run on one console — a
        #: state `ConsoleComposer.ensure` detects, reports, and tells the owner to restart out
        #: of, rather than one it repairs — each memoises its own last value, so after B writes
        #: Y the option stays Y while A's cursor sits on X and A never re-asserts. Before the
        #: coalescing A's next tick would have republished X, so the option flapped between the
        #: two; this makes it stably wrong instead. That is a real narrowing and it is recorded
        #: rather than fixed, because a flapping selection in a console that is already
        #: misassembled is noise rather than a mitigation, and the supported repair for that
        #: state is the restart `ensure` already prescribes. Re-reading the option before each
        #: write would close it at the price of a second tmux round trip per publication.
        #:
        #: `on_unmount`'s skip is the benign half of the same mechanism: a pane exiting with a
        #: memo of `None` leaves whatever the *other* writer published, which is the right
        #: answer while that writer is still alive.
        self._written_selection: SessionId | None = None
        self._ever_written = False

    def _publish_selection(self, session_value: str | None) -> None:
        """This pane owns the console's cursor, so this pane is the one writer of it.

        Scheduled rather than awaited. Publishing is a side effect of the cursor moving, not a
        step in answering a key, and the callers are a message handler and a redraw branch —
        neither may block on a tmux round trip while the owner is still holding an arrow down.

        **Coalesced into a slot rather than issued per call, and that is a correctness fix
        rather than a saving.** Each publication shells out to tmux, a fork/exec with latency
        nothing bounds, and independent workers complete in whatever order the OS returns them.
        Measured: with an earlier write made slower than a later one, the option was left
        naming the row the owner had *left* — and nothing corrects it, so that is simply the
        answer every other pane reads until the cursor moves again. The next stage points
        `alt+s` and `alt+c` at this value with no confirmation, and DEC-007's re-read does not
        cover it: that re-checks whether the *named* session may be stopped, not whether it is
        the one the owner is looking at. A stale-but-live id passes every check and ends the
        wrong agent.

        A failure is swallowed to a log, and what that costs is worth stating exactly rather
        than reassuringly. `set-option` failing does not clear the option, so after one
        successful write a failure leaves the *previous* selection standing while the cursor
        moves on — not "no session selected", which is what an earlier version of this said.
        The next successful publication corrects it, and DEC-007's re-read at issue time is
        what stops a stale-but-parseable id being acted on blindly.

        **Accepted cost, recorded because it has no fix at this layer.** A pane that dies
        without unmounting — SIGKILL, a crash, `tmux kill-pane` — leaves its last selection
        published for the console's lifetime. Two things bound it and neither removes it: the
        option is session-scoped, so it dies with `ra-console` (pinned live), and a sessions
        pane that restarts republishes on its first fill, because the opening draw goes through
        the same funnel. What is not bounded is a console whose sessions pane stays dead: its
        last row remains selected with no cursor anywhere on screen to show it. This is the
        same shape as DEC-062's residual — a hazard reduced to a narrow window rather than
        closed — and Stage 3's chords inherit it.

        **The clean exit has a narrow version of the same hole, measured rather than reasoned.**
        `App._process_messages` cancels every worker before `_shutdown` runs `on_unmount`, and
        cancelling `communicate()` does not kill a `tmux set-option` child that has already
        forked. So: arrow to X, press quit, the worker is cancelled mid-round-trip, `on_unmount`
        acquires the free lock and writes the clear, and the orphan then writes X over it. The
        window is the few milliseconds a fork/exec takes, and nothing at this layer can close
        it — the child is out of the process's hands the moment it exists. Closing it properly
        means the write carrying a sequence tmux could compare, which is a change to the option's
        contract rather than to this method. Named here so Stage 3 inherits a known residual
        rather than an assumption.
        """
        publish = self.services.console_publish_selection
        if publish is None:
            return
        # The screen holds a row *key* -- a string, because that is what an `Option` id is --
        # and the port takes a `SessionId`. Converted here rather than widening the port,
        # because "this is a session" is exactly what the boundary should be asserting.
        # `highlighted_session` already refuses every `\x00`-prefixed sentinel, so a key that
        # will not parse means a row this screen does not understand; it publishes nothing
        # rather than a guess.
        selected: SessionId | None = None
        if session_value is not None:
            try:
                selected = SessionId.parse(session_value)
            except ValueError:
                # Publish *nothing*, rather than return and leave the previous value standing.
                # "Nothing" is the honest answer to a row this screen cannot name; returning
                # would make the last comprehensible row the answer to an incomprehensible one,
                # which is the same defect as not publishing a cleared cursor.
                _LOG.debug("a row key that is not a session id cleared the selection")
        self._pending_selection = selected
        self._selection_pending = True
        self.run_worker(self._write_selection(), name="publish-selection", exit_on_error=False)

    async def _write_selection(self) -> None:
        """Write the pending selection, one writer at a time, latest value wins.

        The lock is what makes the order true. The **slot** is what makes the value true: a
        waiter reads `_pending_selection` rather than a value it captured, so whoever writes
        last writes the newest thing anyone asked for. The loop is coalescing on top of that,
        not correctness — an earlier version of this docstring credited it with the value half,
        and replacing `while` with `if` turns no test red, which is the honest measure of that
        claim. It earns its place by letting one holder absorb a burst rather than handing the
        lock round it.

        Failures are logged here rather than left to Textual's worker channel, which is visible
        only under `textual console`: an operator asking "why did my chords stop following the
        cursor" reads the application's own log.
        """
        publish = self.services.console_publish_selection
        if publish is None:
            return
        async with self._selection_lock:
            while self._selection_pending:
                self._selection_pending = False
                wanted = self._pending_selection
                if self._ever_written and wanted == self._written_selection:
                    continue
                try:
                    await publish(wanted)
                except Exception:
                    _LOG.debug("the console selection could not be published", exc_info=True)
                else:
                    self._written_selection = wanted
                    self._ever_written = True

    async def on_unmount(self) -> None:
        """A pane that is gone has no cursor, so it must not leave one published.

        Awaited rather than scheduled, unlike every other publication here: this is the last
        thing the process does, and a worker started now has nothing left to run on. The option
        lives on the console *session* and outlives this process, so a pane exiting without
        clearing it leaves its final row selected for whatever reads next.
        """
        if self.services.console_publish_selection is None:
            return
        # Through the same slot and the same lock as every other publication, then awaited.
        # Writing directly would race whatever worker is still draining: this clear must be the
        # *last* thing written, and the lock is the only thing that can promise that.
        self._pending_selection = None
        self._selection_pending = True
        await self._write_selection()

    async def action_session_detail(self) -> None:
        """`d` on the highlighted row opens today's detail screen, unchanged."""
        choices = self.query_one("#choices", OptionList)
        index = choices.highlighted
        if index is None or choices.option_count <= index:
            return
        key = choices.get_option_at_index(index).id
        if key is None or key == _BACK:
            return
        await self.tui.show_detail(key)


class SessionDetailScreen(ChoiceScreen):
    """One session's state, what it means, and the actions the policy allows on it."""

    #: always at least Back, plus whatever the policy allows.
    empty_state = NEVER_EMPTY

    def __init__(self, session_value: str, opening_action: str | None = None) -> None:
        super().__init__()
        self.session_value = session_value
        #: One action to perform on arrival, or None. Set by the sessions pane's per-action
        #: keys so that a key there does not have to re-implement anything: it names an
        #: action and this screen performs it through the same `choose` a pressed row uses.
        #:
        #: Consumed exactly once, in `populate`, and cleared *before* it is dispatched --
        #: `populate` runs per mount while `on_reveal` runs on every return, so an action
        #: read by the wrong one would re-ask a destructive question each time the owner came
        #: back from Inspect or from an abort. Clearing first also means a branch that raises
        #: cannot leave it armed.
        self._opening_action = opening_action
        # The session's own name, as the store last reported it. Held rather than re-read on
        # every breadcrumb build because the breadcrumb is drawn from a synchronous property
        # and reading the store is not — `render_detail` refreshes it and says so.
        self._display = ""

    position = "SESSION_DETAIL"

    about_one_session = True

    def subject_session(self) -> str | None:
        """This screen is about one session and holds its id, so a chord acts on that one."""
        return self.session_value

    @property
    def crumb(self) -> str:
        """The session this detail is about, once it has been read; its id until then."""
        return self._display or self.session_value

    #: Its `on_reveal` already re-reads this one session from the shared store on every
    #: back path, so there was something to
    #: re-read here all along. `can_refresh` was first set from "does this screen own a
    #: catalogue-style read" rather than from "is there anything here that goes stale", which
    #: is the question the footer is actually answering. Found by the Stage 1 gate evaluator.
    can_refresh = True

    async def refresh_contents(self) -> None:
        """Ctrl+R does here what coming back to this position does: read it again."""
        await self.on_reveal()

    async def populate(self) -> None:
        self.hide_entry()
        await self.render_detail()
        # Read and cleared in one step, before anything can act on it.
        action, self._opening_action = self._opening_action, None
        if action is None:
            return
        # Scheduled onto the pump, **not awaited here**, and this is a correctness fix rather
        # than a style choice. `ChoiceScreen.on_mount` awaits `populate`, so this method runs
        # *inside* the mount; `confirm_force` and `confirm_remote_control` reach
        # `ask_to_confirm`, which awaits a worker that cannot resolve until the mount has
        # returned and the pump is running. Awaiting the dispatch here therefore deadlocks the
        # app outright -- observed as a test that hung rather than failed, killed at 60s.
        #
        # `call_after_refresh` puts the dispatch on the message pump, which is where every
        # other confirmation on this screen is already raised from: a pressed row reaches
        # `choose` through `on_option_list_option_selected`, a handler. So this makes the
        # opening action arrive by the same route as the keypress it stands in for, which is
        # also what DEC-025 asks -- a confirmation is only ever asked from a screen handler.
        self.post_message(OpeningAction(action))

    async def on_opening_action(self, message: OpeningAction) -> None:
        """The screen handler `OpeningAction` is delivered to. DEC-025's required shape."""
        await self.dispatch_opening(message.action)

    async def dispatch_opening(self, action: str) -> None:
        """Perform an action that arrived from a key rather than from a row.

        **The one entry point for the whole mechanism**, and both callers reach it the same
        way -- `populate` for a freshly pushed detail, `RemoteAgentsTui.show_detail` for one
        already on screen -- because a third caller that forgot either guard below is exactly
        how this goes wrong.

        **Through `choose`, not around it.** Every guard a pressed row gets lives in that
        chain: `confirm_force` holds `holding_the_guard()` across the re-read *and* the whole
        modal and re-checks the policy before asking, `confirm_remote_control` does the same
        for a live pane, and `tui.stop` re-reads and re-checks at issue time -- DEC-007's
        mitigations, and DEC-025's rule that a confirmation is only ever asked from a screen
        handler. Dispatching here rather than calling any of them directly is what keeps this
        an *entry path* rather than a second implementation, which the plan's own research
        names as the highest-risk thing it could have done.

        An action the policy no longer allows is therefore refused by the policy itself, in
        its own words, rather than by a check held here that could drift from it.
        """
        if not self.showing:
            # The owner left between the keypress and the refresh. Logged rather than silent:
            # a discarded intent that leaves no trace turns "I pressed force and nothing
            # happened" into an unanswerable report.
            _LOG.info("the opening action %r was dropped: the detail is no longer showing", action)
            return
        if self.tui.busy:
            # The guard a pressed row already had, on the path that does not go through a row.
            # `ChoiceScreen.on_option_list_option_selected` drops a selection while the surface
            # is busy, and that refusal is load-bearing further down: `app.set_remote_control`
            # has no busy check of its own *because* of it, and its docstring says so --
            # "a second caller reaching this directly would not be refused here, which is the
            # thing to check before adding one". This is that second caller, and this is that
            # check. Without it a key pressed during an in-flight stop could start a second
            # mutating command against the same session, and whichever finished first would
            # clear `busy` while the other was still running.
            _LOG.debug("the opening action %r was refused: a command is already in flight", action)
            return
        await self.choose(action)

    async def on_reveal(self) -> None:
        """Re-read on the way back from Inspect or a confirmation.

        The chain this replaces re-ran the whole detail whenever Escape left one of those,
        so a session whose state moved while the owner was elsewhere came back refreshed.
        """
        await self.render_detail()

    async def render_detail(self) -> None:
        """Show the session's state, re-read from the shared store.

        The record is looked up again rather than trusted from the list: the store has two
        writers, so a session can be stopped elsewhere while this list is on screen.
        """
        tui = self.tui
        try:
            record = await tui.current_record(self.session_value)
        except Exception as error:
            tui.report_store_failure(error, self)
            return
        if record is None:
            self.show_choices(((_BACK, "Back"),))
            self.set_status("That session is no longer available.")
            return
        # The name goes to the header and the state's meaning to the status line. They were
        # three lines in one region, and the first of them — the session's own name — is the
        # part that was true of the whole position rather than of any moment in it, which is
        # exactly the split the breadcrumb exists to take.
        self._display = record.display.rendered
        self.show_breadcrumb()
        self.set_status(
            f"State: {record.state.value}. {explain_state(record.state, record.orphan_provenance)}"
        )
        self.show_choices(self.detail_entries(record))

    def detail_entries(self, record: SessionRecord) -> tuple[tuple[str, str], ...]:
        """The actions this session offers, taken from the policy and not decided here.

        The stop entries are exactly `available_actions(record.state, record.orphan_provenance)`
        in the order it returns them, which puts force last. Adding, filtering, or reordering
        here is what `tests/contract/test_session_actions_parity.py` exists to catch.

        Provenance is passed rather than dropped because an ORPHANED record's rows depend on
        it (DEC-020), and a surface that passed only the state would silently render the
        conservative set — a divergence the parity contract cannot see if the other surface
        does the same thing.
        """
        # The read-only rows below diverge from the bot's on four axes — order, Inspect's
        # capture gate, Copy attach's ownership gate, and the Inspect label. That is
        # deliberate and is enumerated in full at `adapters/telegram/service.py:
        # _detail_reply`, where the sibling set is built. Everything after them is shared
        # policy, so this is the only part of the screen a merge would have to touch.
        entries: list[tuple[str, str]] = [("attach", "Copy attach")]
        if self.services.backend.capture is not None:
            entries.append(("inspect", "Inspect output"))
        # Grouped with the read-only rows above rather than with the stops below, and the bot's
        # twin gives the reason in the same words: renaming changes what the session is called
        # and nothing about what it is doing. Offered in every state for the reason
        # `SessionService.rename` does not gate on one — naming a session that has just ended is
        # harmless, and the row it is on is still listed until reconciliation removes it.
        entries.append(("rename", "Rename"))
        # No "Trust this project" row, deliberately: DEC-047. The console exchanges its left
        # pane with the agent's (DEC-040), so the owner is looking at the trust dialog and
        # answers it there. `trust_available` is the shared policy and still says yes for
        # these records -- it is the *bot* that acts on it, where there is no pane.
        # One row per direction, so the decision is taken here and the confirmation that
        # follows has exactly one thing to confirm. The single "Claude Remote Control" row
        # this replaces opened a three-row screen where Enable and Disable sat side by side
        # under a heading — a chooser wearing a confirmation's clothes. Which directions
        # those are is now the shared policy's answer rather than a fixed pair, so the two
        # surfaces cannot drift on it.
        entries.extend(remote_control_entries(record))
        entries.extend(
            (action, ACTION_LABELS[action])
            for action in available_actions(record.state, record.orphan_provenance)
        )
        entries.append((_BACK, "Back"))
        return tuple(entries)

    async def choose(self, key: str) -> None:
        if key == _BACK:
            await self.tui.go_back()
        elif key == "attach":
            await self.show_attach()
        elif key == "inspect":
            await self.show_inspect()
        elif key == "rename":
            await self.show_rename()
        elif key in _REMOTE_CONTROL_DIRECTIONS:
            await self.confirm_remote_control(_REMOTE_CONTROL_DIRECTIONS[key])
        elif key == FORCE:
            await self.confirm_force()
        elif key in ACTION_LABELS and key != FORCE:
            # The `key != FORCE` is redundant with the branch above and deliberately kept:
            # FORCE is a member of ACTION_LABELS, so without it the only thing stopping a
            # single keypress from force-stopping is the *order* of these two branches.
            # Restructuring this chain into a dispatch table would silently remove the
            # confirmation step, and no existing test asserts the ordering itself.
            await self.tui.stop(key, self.session_value, self)

    async def confirm_force(self, session_value: str | None = None) -> None:
        """Re-read the record, ask the modal, and issue only on a `True`.

        **The parameter matches `ChoiceScreen.confirm_force`'s and is deliberately unused.**
        Since the Alt layer, `ChoiceScreen.on_row_stop_action` is inherited by this screen and
        calls `self.confirm_force(message.session_value)` — so a zero-argument override here
        raised `TypeError` out of a message handler the moment `alt+f` was pressed on a detail,
        which exits the app. Found by Task 3.2's Tier-1 review.

        Ignoring it rather than preferring it is the deliberate half. On this screen the chord
        resolves through `subject_session()`, which *is* `self.session_value`, so the two are
        equal by construction; and if they ever were not, forcing the session this screen is
        describing is the safe direction — the modal, the action and what the owner is looking
        at stay the same session. Preferring the argument would let a caller kill something the
        screen never showed.

        Guarded across the read *and* the whole modal, and this guard is load-bearing twice
        over. `action_back` runs on the app's pump while this runs on the screen's, so without
        it an Escape landing inside the read pops *this* screen — and then the modal is pushed
        onto whatever the pop revealed, describing a session the position beneath it is no
        longer showing. Worse, the `set_status` below would be called on a screen that has
        already been unmounted, raising `NoMatches` out of the very path that exists to report
        a vanished session without losing the app.

        Holding it *across* the question, rather than releasing once the modal is up, is what
        closes the window between the two: `ask_to_confirm` yields to the pump before the
        modal is mounted, and an Escape delivered in that gap would pop this screen out from
        under a question already on its way. Nothing is lost by holding it — under a modal the
        app's own bindings are not in the binding chain at all, so there is no second action
        the guard could be refusing.

        The guard is released before the stop, because `stop` takes it itself and refuses
        outright when it is already held. It is *not* released before the abort's re-read:
        that refresh awaits a store read, and between the release and the redraw the detail
        is showing its pre-modal rows with the cursor still on Force stop and nothing refusing
        a keypress — so a second enter opened a second confirmation on top of the first one's
        refresh. Each stacked question still needed its own yes, so nothing could be killed by
        it, but it is the same await-then-render window `showing` and this guard exist to
        close everywhere else in this file. Found by the stage's deep review.
        """
        async with self.holding_the_guard():
            record = await self.tui.current_record(self.session_value)
            if record is None:
                await self.refuse()
                return
            if FORCE not in available_actions(record.state, record.orphan_provenance):
                # Asked before the question rather than only after the answer. `stop` re-checks
                # regardless — that is DEC-007's fourth mitigation and it is what makes this
                # safe rather than necessary — but a surface that opens a kill confirmation it
                # already knows it will refuse is asking the owner to authorise nothing.
                await self.refuse(
                    f"{ACTION_LABELS[FORCE]} is no longer available for this session. "
                    f"{explain_state(record.state, record.orphan_provenance)}"
                )
                return
            if not self.showing:
                return
            try:
                confirmed = await self.tui.ask_to_confirm(ForceConfirmModal.for_record(record))
            except Exception as error:
                # `ask_to_confirm` unwraps a failed worker and re-raises, and this call runs
                # inside a message handler — where an escaping exception exits the app. Every
                # other awaited read on this screen already reports rather than raises; this
                # one is newer, not different.
                _LOG.exception("the force confirmation could not be shown")
                self.announce(f"The confirmation could not be shown: {error} Nothing was stopped.")
                return
            if not confirmed:
                # Abort re-reads, exactly as leaving the confirmation screen used to: the owner
                # may have opened it only to look, and the session can have moved on while it
                # was open.
                await self.on_reveal()
                return
        await self.tui.stop(FORCE, self.session_value, self)

    async def confirm_remote_control(self, desired: RemoteControlState) -> None:
        """Ask before changing a live pane's control mode, re-checking the policy first.

        Guarded, answered and released for the reasons given on `confirm_force`, and to the
        same shape: read under the guard, check the policy, ask, refresh on an abort without
        letting go, and take the guard off only for the call that takes it itself.

        An earlier version of this sentence claimed the two methods mirrored each other "line
        for line", and they did not — this one re-checked its policy before asking and
        `confirm_force` did not. That was true the day it was written, which is the useful
        part of the story: a claim of symmetry is a claim about two things at once and goes
        stale when either moves. They are symmetric now because `confirm_force` gained the
        check, not because the sentence was softened.

        The policy is re-checked here *and* again inside `set_remote_control`. That is not
        redundant — this check decides whether to ask at all, and that one decides whether to
        act on the answer, with the modal's whole open duration in between.
        """
        async with self.holding_the_guard():
            record = await self.tui.current_record(self.session_value)
            if record is None:
                await self.refuse()
                return
            if not remote_control_available(record):
                await self.refuse(
                    "Remote Control is not available for this session. "
                    f"{explain_state(record.state, record.orphan_provenance)}"
                )
                return
            if not self.showing:
                return
            try:
                confirmed = await self.tui.ask_to_confirm(
                    RemoteControlConfirmModal.for_change(record, desired)
                )
            except Exception as error:
                _LOG.exception("the Remote Control confirmation could not be shown")
                self.announce(f"The confirmation could not be shown: {error} Nothing was changed.")
                return
            if not confirmed:
                await self.on_reveal()
                return
        await self.tui.set_remote_control(self.session_value, desired, self)

    async def show_attach(self) -> None:
        """Render the command that reaches this pane, or say why there is none.

        The affordance is always offered and answers when chosen, rather than being hidden
        when unavailable. Hiding it is what the bot does, and it leaves the owner unable to
        tell a dead pane from a surface that simply forgot to draw the button.
        """
        async with self.holding_the_guard():
            try:
                record = await self.tui.current_record(self.session_value)
                if record is None:
                    await self.refuse()
                    return
                command = await self.services.backend.sessions.copy_attach(record.session_id)
            except Exception as error:
                self.tui.report_store_failure(error, self)
                return
        if command is None:
            # "no pane left", not "not live": a preserved pane attaches read-only now
            # (DEC-021), so liveness stopped being what this refusal turns on. Saying it still
            # did would send an owner looking for a way to revive a session whose output is
            # sitting right there.
            self.announce(
                "Attach is not available: this session has no pane on this host any more, or "
                f"the pane found for it belongs to a different project or agent. "
                f"{explain_state(record.state, record.orphan_provenance)}",
                severity="warning",
            )
            return
        # **Copied as well as shown, and neither half is redundant.** The affordance has been
        # called "Copy attach" since it was written and until now it only ever *printed* the
        # command; `App.copy_to_clipboard` writes it over OSC 52, which is what makes the name
        # true and works through SSH and inside tmux.
        #
        # The printed command stays, because OSC 52 is the half that can silently fail. Some
        # terminals ignore the sequence outright — Textual's own docstring names macOS Terminal
        # — and a clipboard write reports nothing back either way. On a session that did not
        # come up, this string is the only handle left on a pane that may still be live, so the
        # fallback is load-bearing rather than belt-and-braces. It stays in the status line and
        # not in a toast for the same reason: a toast expires under the owner mid-copy.
        self.tui.copy_to_clipboard(command)
        self.set_status(f"Attach with: {command}")
        # Worded as an attempt, not an outcome. The surface cannot observe whether the terminal
        # accepted the sequence, and sub-plan 3 spent a stage on the general form of this
        # mistake: a message that asserts what only the other end could confirm.
        self.announce(
            "Sent to the clipboard — not every terminal accepts that, so the command is "
            "on screen too.",
            severity="information",
        )

    async def show_inspect(self) -> None:
        """Capture this session's output, then open it on a screen of its own.

        The capture runs *before* the push, deliberately: a capture that fails must report
        onto this detail and leave the owner here, rather than opening an output screen with
        nothing in it and an error message they would have to leave to read.
        """
        capture = self.services.backend.capture
        if capture is None:
            return
        async with self.holding_the_guard():
            record = await self.tui.current_record(self.session_value)
            if record is None:
                await self.refuse()
                return
            try:
                async with self.awaiting("Capturing the session's output…"):
                    captured = await capture(record.session_id)
            except Exception as error:
                _LOG.exception("capture failed")
                self.announce(f"The output could not be captured: {error}")
                return
            text = capture_for_pane(captured, self.services.capture_redactions)
            await self.advance_to(InspectScreen(text or "This session has produced no output yet."))

    async def show_rename(self) -> None:
        """Re-read the session, then open the entry that names it.

        The read happens *before* the push for the reason `show_inspect` gives: a session that
        has gone must be reported onto this detail, not onto an entry the owner would have to
        leave in order to read why their typing went nowhere.
        """
        async with self.holding_the_guard():
            record = await self.tui.current_record(self.session_value)
            if record is None:
                await self.refuse()
                return
            await self.advance_to(RenameScreen(self.session_value))


class RenameScreen(ChoiceScreen):
    """One optional name for a session that already exists.

    **This is where naming a session lives, and the launch wizard is where it used to.** A
    label chosen before the launch is chosen before there is anything to look at, and it could
    never be changed afterwards — so the local surface had the naming step at the one moment
    the owner knows least, and the bot, which has no such step, could rename at will. DEC-007
    makes the local terminal a full control plane rather than a launch wizard; this row is the
    last post-launch capability it was missing.

    **`entry_is_a_commitment` is set, and it was first written here as `False` on an argument
    that did not survive contact with the invariant.** That argument was that the two screens
    already declaring it carry their typed value forward into a further step which holds it,
    whereas `submit` here mutates outright — so before enter there is one retypable string and
    nothing assembled. `test_every_screen_that_commits_typed_text_declares_it` rejects that
    reasoning, and is right to: the rule it pins is `entry_is_a_commitment == hasattr(screen,
    "submit")`, and what the flag protects is typed text a global key would discard silently,
    which this screen has as much as the project-name entry does. The distinction drawn above
    is real but is not the one the flag turns on.
    """

    #: a text entry, not a list.
    empty_state = NEVER_EMPTY

    position = "RENAME"
    filter_placeholder = "New name"
    # Typed here and committed by `submit`; leaving discards it.
    entry_is_a_commitment = True
    #: Its own name rather than the choice that led here: the detail one level down already
    #: carries the session, and a trail that repeats its own last entry says nothing twice.
    crumb = "Rename"

    def __init__(self, session_value: str) -> None:
        super().__init__()
        self.session_value = session_value

    about_one_session = True

    def subject_session(self) -> str | None:
        """This screen is about one session and holds its id, so a chord acts on that one."""
        return self.session_value

    async def populate(self) -> None:
        self.set_status("Enter a name for this session, then press enter. Leave empty to keep it.")
        # `valid_empty` left at its default: an empty entry is the documented way to leave the
        # name alone, so the box must not open refusing the value it is about to be given.
        self.text_entry(
            "New name",
            validators=[LabelWithinBound(self.services.max_label_length)],
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        """Say the bound is broken at the keystroke that broke it, not at the enter after it."""
        event.stop()
        self.announce_rejection(event.validation_result)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        await self.submit(event.value)

    async def submit(self, value: str) -> None:
        """Validate, then rename — re-reading the session first, because it may have gone.

        Awaited inline under the guard rather than run on a worker, matching this screen's
        other short store calls rather than `ProjectReviewScreen._create_project`. That
        screen went off the pump because creating a project scans the development root and
        writes a directory, so
        holding the pump made `ctrl+q` unanswerable for the duration. A rename is one indexed
        UPDATE under the session lock — the same cost class as the record read directly above
        it, which is already awaited here.

        **A repeat is refused by two checks together, and an earlier version of this paragraph
        credited the busy guard alone — which cannot do it here on its own.** `tui.busy` is
        consulted by
        `check_action` and by `on_option_list_option_selected`, so it drops a repeated *row*
        selection; nothing on the `Input.Submitted` dispatch path reads it, and `awaiting`
        covers `#choices` rather than the entry. This is the surface's only mutating submit, and
        it was written without the check its sibling committing entry has
        (`NameScreen.submit`; the launch flow's label entry had it too, and has since been
        removed along with the step).

        **Both checks below are load-bearing and they cover different windows**, which is the
        division `holding_the_guard` states when it says both are kept — the guard is the
        narrow fix for paths that can afford to block, and `showing` covers every path
        including the ones that cannot:

        - `showing` catches the repeat the screen's pump actually delivers. Two Enters are
          handled in order, so the second runs on a screen the first has already left.
          Renaming twice writes the same label and is invisible; popping twice is not — it
          lands the owner on the sessions list instead of the session they just named.
        - `tui.busy` catches a submit starting while another is still suspended, which
          `showing` cannot see because this screen is still on top throughout. Measured
          reachable rather than assumed: gating the store call and starting two submits puts
          two renames through, and `test_a_second_enter_arriving_mid_rename_is_dropped_too`
          holds that shut. This is the check `on_option_list_option_selected` already applies
          to every mutating *row*; the entry simply never had it.

        Together that is DEC-008's shape — drop the repeat, never cancel the one in flight —
        enforced rather than asserted.
        """
        if not self.showing or self.tui.busy:
            return
        try:
            label = label_or_error(value, self.services.max_label_length)
        except ValueError as error:
            # A toast rather than the status line, which still holds the instruction the owner
            # is in the middle of following. Overwriting it would leave them told what was
            # wrong and no longer told what to do about it.
            self.announce(str(error), severity="warning")
            return
        if label is None:
            # Declining to name is not the same act as clearing a name, and only one of the two
            # is offered. The store supports `set_label(None)` and no screen on either surface
            # reaches it — the bot's Skip is explicit that clearing on an empty entry would make
            # this the only way to lose a name and would do it by accident.
            await self.tui.go_back()
            return
        async with self.holding_the_guard():
            record = await self.tui.current_record(self.session_value)
            if record is None:
                # The session ended under the owner while the box was open. Its detail is gone
                # too, so the list is the only honest place to land — the same answer the bot's
                # rename gives, for the same reason.
                self.announce("That session is no longer available.", severity="warning")
                await self.tui.show_sessions()
                return
            try:
                async with self.awaiting("Renaming…"):
                    await self.services.backend.sessions.rename(record.session_id, label)
            except Exception as error:
                self.tui.report_store_failure(error, self)
                return
        # Asked again after the await, not only on entry. `go_back` pops whatever is on top and
        # has no liveness check of its own, so a screen left during the store read would pop
        # somebody else's position.
        if not self.showing:
            return
        # Not `render_detail` on the screen beneath: `go_back` pops and awaits that screen's
        # own `on_reveal`, which re-reads this session from the store. Reaching past the pop to
        # redraw would show the record this method already has, which is the one thing that
        # cannot prove the write landed.
        await self.tui.go_back()


class InspectScreen(ChoiceScreen):
    """This session's captured output: scrollable, jumpable, and searchable.

    **All three of those verbs are this screen's own work, and two of them were assumed.**
    The sub-plan's research recorded that a read-only `TextArea` "gives selection, search, and
    scroll-to-end", and the stage goal was written on that premise. Measured against the
    pinned Textual 8.2.8, it gives selection and line-by-line movement and neither of the
    other two: its 32 bindings contain no find action and no document-start or document-end
    action, so `ctrl+f`, `/`, `f3`, `ctrl+end` and `ctrl+home` were all inert here, and
    reaching the bottom of a long capture took 105 `pagedown` presses. A gate evaluator drove
    it and counted them.

    So the keys are bound here rather than the goal being quietly reinterpreted as the one
    verb the widget happened to supply. The newest output of an agent is at the *bottom*,
    which is what makes the missing jump the sharper of the two absences.
    """

    #: Shows the output pane, never rows.
    empty_state = NEVER_EMPTY

    position = "INSPECT"

    #: About one session, and unable to say which — the constructor takes the captured output
    #: alone, because the detail one level down the stack names the session in its own crumb.
    #:
    #: Declared anyway, and `subject_session` left answering `None`, which is a **refusal**: a
    #: chord pressed while reading session A's output must not act on whatever the sessions
    #: pane highlights. That is the same defect as on the detail, and the fact that this screen
    #: cannot name its subject makes it worse to guess, not safer.
    about_one_session = True
    status = (
        "Output. / to find, n and N to step, ctrl+home and ctrl+end to jump, escape to go back."
    )
    #: "Output", not the session's name: the detail one level down the stack already carries
    #: that, and a trail that repeats its own last entry says nothing twice.
    crumb = "Output"
    filter_placeholder = "Find in output"

    BINDINGS = [
        Binding("slash", "find", "Find", tooltip="Search this capture"),
        Binding("n", "next_match", "Next match", show=False),
        Binding("N", "previous_match", "Previous match", show=False),
        Binding("ctrl+end", "to_end", "End", tooltip="Jump to the newest output"),
        # Hidden from the footer, not from the surface. Three new entries here overflowed the
        # bar at 80 columns and clipped `Resume` to `Resum` — a binding this screen did not
        # add, silently truncated by one that did, which is a worse trade than an unlisted
        # key. `ctrl+end` is the half that matters (an agent's newest output is at the
        # bottom); its inverse is named in the status line above, where there is room to say
        # both. Measured against the committed 80-column baseline, not assumed.
        Binding(
            "ctrl+home",
            "to_start",
            "Start",
            tooltip="Jump to the top of the capture",
            show=False,
        ),
    ]

    def __init__(self, output: str) -> None:
        super().__init__()
        # The session's name used to be the other half of this constructor, prepended to a
        # two-line status. It is gone rather than moved: the detail one level down the stack
        # names the session in its own crumb, so passing it here would have been carrying a
        # value only to render it twice.
        self._output_text = output
        #: Line indices matching the current query, and where in that list the cursor sits.
        #: Recomputed per query rather than incrementally, because a capture is immutable
        #: once shown — there is no edit for an index to drift against.
        self._matches: tuple[int, ...] = ()
        self._match_index = 0
        self._query = ""

    async def populate(self) -> None:
        self.hide_entry()
        self.show_choices(())
        self.show_output(self._output_text)

    @property
    def _pane(self) -> TextArea:
        return self.query_one("#output", TextArea)

    def action_find(self) -> None:
        """Reveal the entry as a find box, reusing the widget every screen already composes."""
        entry = self.query_one("#filter", Input)
        entry.display = True
        entry.placeholder = self.filter_placeholder or ""
        entry.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Search as the query is typed, and land on the first match without waiting for enter."""
        event.stop()
        self._search(event.value)
        if self._matches:
            self._match_index = 0
            self._reveal_match()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter hands the keyboard back to the pane, so n and N work immediately."""
        event.stop()
        self.query_one("#filter", Input).display = False
        self._pane.focus()
        if not self._matches and self._query:
            self.announce(f"No line matches {self._query!r}.", severity="warning")

    def _search(self, query: str) -> None:
        self._query = query
        if not query:
            self._matches = ()
            self.set_status(self.status)
            return
        folded = query.casefold()
        self._matches = tuple(
            index
            for index, line in enumerate(self._output_text.splitlines())
            if folded in line.casefold()
        )
        if not self._matches:
            self.set_status(f"No match for {query!r}. Escape to go back.")

    def _reveal_match(self) -> None:
        if not self._matches:
            return
        line = self._matches[self._match_index]
        pane = self._pane
        # Moving the cursor is what scrolls a `TextArea`; there is no scroll-to-line that also
        # marks where the owner is. `(line, 0)` rather than the match column, so a wrapped hit
        # puts the start of its line on screen instead of the middle of it.
        pane.move_cursor((line, 0))
        pane.scroll_cursor_visible(center=True)
        self.set_status(
            f"Match {self._match_index + 1} of {len(self._matches)} for {self._query!r} "
            f"— line {line + 1}. n and N to step."
        )

    def action_next_match(self) -> None:
        if not self._matches:
            return
        self._match_index = (self._match_index + 1) % len(self._matches)
        self._reveal_match()

    def action_previous_match(self) -> None:
        if not self._matches:
            return
        self._match_index = (self._match_index - 1) % len(self._matches)
        self._reveal_match()

    def action_to_end(self) -> None:
        """The tail, which is where an agent's newest output is."""
        pane = self._pane
        lines = self._output_text.splitlines()
        pane.move_cursor((max(0, len(lines) - 1), 0))
        pane.scroll_cursor_visible()

    def action_to_start(self) -> None:
        self._pane.move_cursor((0, 0))
        self._pane.scroll_cursor_visible()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Step-to-match is only a key when there is a match list to step through."""
        if action in {"next_match", "previous_match"}:
            return bool(self._matches)
        return super().check_action(action, parameters)


def capture_for_pane(captured: str, redactions: tuple[str, ...]) -> str:
    """Turn a raw capture into what the output pane should show.

    `application/captures.render_capture` is the shared bounded rendering, so nothing is
    re-implemented here — including the bounds, which it takes from this surface rather than
    holding any of its own. What is deliberately *not* reused is the Telegram presentation
    wrapper: its 4096-UTF-16-unit inline cap and session-output.txt attachment fallback exist
    because Telegram messages are bounded, and a scrollable local pane is not.

    **Named for the pane rather than for the rendering**, so that `render_capture` means one
    thing across the project. This was `render_capture` too, which made the shared function
    something this module had to import under an alias — two functions, one name, different
    signatures, one calling the other. The Stage 3 gate's own sweep for a second definition is
    what surfaced it: a collision that has to be explained in a comment is one a reader has to
    re-resolve every time.

    The shared renderer only *signals* that a capture was binary, because the two surfaces
    refuse in different sentences. This one is the pane's, worded for a full screen; the bot
    words its own.
    """
    rendered = render_capture(
        captured.encode(),
        max_lines=_INSPECT_MAX_LINES,
        max_bytes=_INSPECT_MAX_BYTES,
        redactions=redactions,
    )
    if rendered.text is None:
        # Matching the bot's refusal, for the same reason: a pane emitting NUL is not
        # rendering text, and printing it to a terminal can corrupt the display.
        return "This session's output is binary and cannot be displayed."
    return rendered.text
