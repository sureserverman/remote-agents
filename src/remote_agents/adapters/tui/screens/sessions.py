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

import logging

from textual.binding import Binding
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Input, OptionList, TextArea

from remote_agents.adapters.tui.model import _BACK, label_or_error, session_row
from remote_agents.adapters.tui.screens.base import NEVER_EMPTY, ChoiceScreen, held_option_id
from remote_agents.adapters.tui.screens.confirm import (
    ForceConfirmModal,
    RemoteControlConfirmModal,
)
from remote_agents.adapters.tui.screens.validation import LabelWithinBound
from remote_agents.application.captures import render_capture
from remote_agents.application.session_actions import (
    ACTION_LABELS,
    FORCE,
    REMOTE_CONTROL_LABELS,
    available_actions,
    explain_state,
    remote_control_available,
    remote_control_directions,
)
from remote_agents.domain.models import SessionRecord
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
#: Nothing here decides whether an action is *legal*. The key names it, the detail performs it
#: through `choose`, and the policy re-checked at issue time is what refuses -- DEC-007's
#: fourth mitigation. A key is only ever a faster way to reach a row that already exists.
#:
#: That chain asks before `force` and before either Remote Control direction, and does **not**
#: ask before Stop and close or Clean up. Stated precisely rather than as "the same
#: confirmations a row gets", which was the first version of this comment and was the sentence
#: a later reader would have used to conclude this path was already safe for all six. It is the
#: reason `UNCONFIRMED_MUTATING_ACTIONS` below exists.
#:
#: No trust key, deliberately (DEC-047): this surface answers the trust question in the pane
#: the console exchanges in, so it has no trust row and must not grow a trust key either.
SESSION_ACTION_KEYS: tuple[tuple[str, str, str], ...] = (
    ("a", "attach", "Copy attach"),
    ("i", "inspect", "Inspect output"),
    ("r", "rename", "Rename"),
    ("f", FORCE, ACTION_LABELS[FORCE]),
)

#: The actions a key must never carry: exactly the branch of `SessionDetailScreen.choose` that
#: reaches `tui.stop` **without asking** -- `key in ACTION_LABELS and key != FORCE`, which today
#: is Stop and close, and Clean up. Derived from that condition rather than listing the two, so
#: a third unconfirmed action added to the policy is excluded here the day it appears.
#:
#: **The plan proposed `s` and `c` for these, and they are deliberately not bound.** Two
#: decisions close off every way to do it safely:
#:
#: - DEC-018 -- "neither surface gains a confirmation for graceful stop or for cleanup",
#:   applied "to both surfaces or neither". So the key cannot ask.
#: - DEC-007 -- every screen rests its cursor on a non-mutating entry. So the key cannot open
#:   the detail with the stop row under the cursor either.
#:
#: What made DEC-018's accepted cost -- "an owner who stops the wrong session loses that output
#: with no second chance" -- tolerable was that reaching those rows took two deliberate,
#: human-paced keypresses: Enter to open the detail, then Enter on a row the owner could see.
#: A key collapses that to one, and this list auto-refreshes every ten seconds restoring the
#: cursor *by key* -- so a session that ends between ticks drops its row and the cursor falls
#: to row 0, a different session, silently. Pressed at that moment, an auto-dispatched `s`
#: would gracefully stop a live agent the owner never selected, with nothing asked and nothing
#: recoverable. Found by this task's Tier-1 review.
#:
#: `d` still reaches them in two keypresses, which is what it did before this task.
UNCONFIRMED_MUTATING_ACTIONS = frozenset(ACTION_LABELS) - {FORCE}

#: The Remote Control key, kept out of the table above because it is the one key whose action
#: is not known until the record is read -- see `action_row_remote_control`.
_REMOTE_CONTROL_KEY = "m"

#: What the status line says about the row keys, built from the table rather than written
#: beside it. Both sessions positions need the sentence and the second is a subclass of the
#: first, so a literal in each would be two strings to keep agreeing -- the shape
#: `remote_control_entries` above already exists to avoid.
#:
#: Short words on purpose. The full labels ran the pane's status past what `#status` can show
#: at 60 columns, where it clips with no ellipsis at all rather than eliding -- worse than the
#: truncation this region was hardened against once already.
#:
#: **A sixth key would have to earn its columns, and a test says so rather than this comment.**
#: `test_the_status_line_names_the_keys_and_is_not_truncated` renders the real status at 100,
#: 80 and 60 and asserts every key survives -- 60 being the floor `app.py`'s own budget comment
#: commits to. Measured today: 100 characters on the full position and 112 on the pane, against
#: a two-row 60-column budget of 120. Grow this table and that test fails at 60 first.
_ROW_KEY_SUMMARY = " · ".join(
    [
        *(f"{key} {action}" for key, action, _label in SESSION_ACTION_KEYS),
        f"{_REMOTE_CONTROL_KEY} remote",
    ]
)


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
        for key, action, label in SESSION_ACTION_KEYS
    ),
    Binding(_REMOTE_CONTROL_KEY, "row_remote_control", "Claude Remote Control", show=False),
]

# The invariant, enforced where it cannot be skipped. It was previously asserted only by a
# test, and this codebase already has the better pattern for exactly this shape --
# `ChoiceScreen.__init_subclass__` raises at class-definition time rather than trusting a test
# to be run. The failure this guards is not hypothetical and not small: re-adding `s` here to
# "finish the job the plan proposed" would put an unconfirmed graceful stop one keypress from
# a list whose cursor moves under a 10-second refresh, and it would ship the moment someone
# did not notice one red test.
_bindable = {action for _key, action, _label in SESSION_ACTION_KEYS}
if _bindable & UNCONFIRMED_MUTATING_ACTIONS:  # pragma: no cover - import-time invariant
    raise RuntimeError(
        "these keys would auto-perform an action the detail never asks about: "
        f"{sorted(_bindable & UNCONFIRMED_MUTATING_ACTIONS)}. "
        "DEC-018 forbids confirming them and DEC-007 forbids resting the cursor on them, so a "
        "key cannot carry one safely; `d` reaches them in two keypresses."
    )
del _bindable

#: The console key, kept off `SESSION_ACTION_BINDINGS` deliberately -- see
#: `SessionsPaneScreen.BINDINGS`, which is the only position that offers it.
_SHOW_PROJECTS_BINDING = Binding("p", "show_projects_pane", "Projects", show=False)


class _SessionActionKeys:
    """The per-action key *behaviour* both sessions positions share.

    Methods only. The bindings that reach them are attached to the screen classes for the MRO
    reason recorded above; what lives here is the one definition of what a key does, so
    `SessionsPaneScreen` inherits it rather than holding a second copy.
    """

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
        choices = self.query("#choices").first(OptionList) if self.query("#choices") else None
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
        if action in {"row_action", "row_remote_control"}:
            return self.highlighted_session() is not None
        if action == "show_projects_pane":
            # Absent, not inert, on a host that wired no console. The owner cannot tell a key
            # that quietly does nothing from a surface that forgot to draw it, and this one is
            # about *where things are on screen* -- the failure would look like the console
            # being broken rather than the key being unavailable.
            return self.services.console_show_projects is not None
        return super().check_action(action, parameters)

    async def action_row_action(self, action: str) -> None:
        """Open the highlighted session's detail, asking it to perform `action`.

        The whole of what a key does. It carries no confirmation, no policy check and no
        command of its own: those live on the detail, once, and this is an entry to them.
        """
        session_value = self.highlighted_session()
        if session_value is None or self.tui.busy:
            # `busy` mirrors `ChoiceScreen.on_option_list_option_selected`, which refuses a
            # pressed row while a command is in flight. `dispatch_opening` checks it again once
            # the detail exists; this is the same refusal one step earlier, so the two entry
            # paths agree rather than relying on the pump staying serialized forever.
            return
        await self.tui.show_detail(session_value, action)

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
        try:
            record = await self.tui.current_record(session_value)
        except Exception as error:
            self.tui.report_store_failure(error, self)
            return
        if not self.showing:
            # The owner left while the store was answering. Every other post-await continuation
            # on this screen family re-checks this before acting -- `dispatch_opening`,
            # `confirm_force`, `confirm_remote_control`, `show_attach` -- because `action_back`
            # only consults the app-level busy flag, and this method sets none. Without it a
            # read landing late pushes a detail onto whatever the owner navigated to instead.
            return
        if record is None:
            await self.tui.show_detail(session_value)
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
        await self.tui.show_detail(session_value, opening)


class SessionsScreen(_SessionActionKeys, ChoiceScreen):
    """Every managed session, including ones this process never launched.

    The one position in this surface whose answer goes stale with nobody touching it: the
    store has a second writer — the bot, and any reconcile the host runs — so a session can
    start, stop or be reconciled while the owner sits here reading. Ctrl+R has re-read it on
    demand since sub-plan 3; this screen now also re-reads itself on an interval, and stops
    doing so the moment it is not the screen on top.
    """

    BINDINGS = list(SESSION_ACTION_BINDINGS)

    empty_state = "No managed sessions on this host."

    position = "SESSIONS"
    can_refresh = True
    crumb = "Sessions"

    #: What this position tells the owner a row does, and where an empty list sends them.
    #: Class attributes rather than literals at the call site because the console's sessions
    #: *pane* means something different by Enter and has nowhere to escape to — and a status
    #: describing the other surface's keys is a false sentence, not a cosmetic one.
    listing_status = "{count} managed session(s). Select one for detail, or " + _ROW_KEY_SUMMARY
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

    async def populate(self) -> None:
        self.hide_entry()
        await self.reload()
        # Started here rather than in an `on_mount` of this screen's own: the base class makes
        # `on_mount` a template method precisely so a screen cannot forget the chrome by
        # defining one, and `populate` is the hook it leaves for exactly this.
        if self._auto is None:
            self._auto = self.set_interval(_SESSIONS_AUTO_REFRESH, self._auto_reload)

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
          command — but those are issued from the *detail* screen, which suspends this timer
          by being pushed on top, so that check is the belt and the suspension is the braces.
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
        """
        self._visit += 1
        await self.reload()

    async def refresh_contents(self) -> None:
        """Re-run readiness and the listing, which is what Refresh means on this screen.

        The same work as `on_reveal`, and this is the position where the key earns its place:
        the store has a second writer, so this list can go stale with the owner sitting on it
        and no navigation to trigger a re-read. Until this task, Ctrl+R here re-read the
        project catalogue and unwound to the project picker.
        """
        await self.reload()

    async def reload(self) -> None:
        """Refresh readiness, then list what the shared store actually holds — on request.

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
        self._draw_listing(records)

    def _draw_listing(
        self, records: tuple[SessionRecord, ...], *, keep_cursor: bool = False
    ) -> None:
        """Draw a listing, optionally leaving the cursor on the row it was already on.

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
        if not records:
            self.show_choices(())
            self.set_status(self.empty_status)
            return
        self.set_status(self.listing_status.format(count=len(records)))
        rows = tuple((str(record.session_id), session_row(record)) for record in records)
        if not keep_cursor:
            self.show_choices(rows)
            return
        # Restore by row *key*, not by index. A session that ended between two ticks shortens
        # the list above the cursor, so the index the owner was on now names a different
        # session — and this list's rows are the handles on the stop actions one screen
        # deeper. A key that has gone falls back to row 0, which is the same non-mutating
        # resting position every other fill uses (DEC-007).
        choices = self.query_one("#choices", OptionList)
        current = held_option_id(choices)
        keys = [key for key, _text in rows]
        highlight = keys.index(current) if current in keys else 0
        self.show_choices(rows, focus=choices.has_focus, highlight=highlight)

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

    The resting cursor stays on a non-mutating row (DEC-007, BL-004) and this pane satisfies
    that by what Enter *is*: an exchange writes no record and touches no lifecycle (DEC-040).
    Every mutating action is behind `d`.
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
    listing_status = (
        "{count} managed session(s). Enter opens one, d for its detail, or " + _ROW_KEY_SUMMARY
    )
    empty_status = "No managed sessions on this host. Launching one starts it here."
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

    async def confirm_force(self) -> None:
        """Re-read the record, ask the modal, and issue only on a `True`.

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
