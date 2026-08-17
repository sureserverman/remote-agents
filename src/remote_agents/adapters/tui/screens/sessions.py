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
from textual.timer import Timer
from textual.widgets import Input, OptionList, TextArea

from remote_agents.adapters.tui.model import _BACK, label_or_error, session_row
from remote_agents.adapters.tui.screens.base import NEVER_EMPTY, ChoiceScreen
from remote_agents.adapters.tui.screens.confirm import (
    ForceConfirmModal,
    RemoteControlConfirmModal,
)
from remote_agents.adapters.tui.screens.validation import LabelWithinBound
from remote_agents.application.session_actions import (
    ACTION_LABELS,
    FORCE,
    REMOTE_CONTROL_LABELS,
    available_actions,
    explain_state,
    remote_control_available,
    remote_control_directions,
    trust_available,
)
from remote_agents.domain.models import SessionRecord
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.domain.trust import TrustState
from remote_agents.ports.terminal_text import sanitize_terminal_text

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


class SessionsScreen(ChoiceScreen):
    """Every managed session, including ones this process never launched.

    The one position in this surface whose answer goes stale with nobody touching it: the
    store has a second writer — the bot, and any reconcile the host runs — so a session can
    start, stop or be reconciled while the owner sits here reading. Ctrl+R has re-read it on
    demand since sub-plan 3; this screen now also re-reads itself on an interval, and stops
    doing so the moment it is not the screen on top.
    """

    empty_state = "No managed sessions on this host."

    position = "SESSIONS"
    can_refresh = True
    crumb = "Sessions"

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
            self.set_status("No managed sessions. Press escape to return to the project list.")
            return
        self.set_status(f"{len(records)} managed session(s). Select one for detail.")
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
        resting = choices.highlighted
        current = (
            choices.get_option_at_index(resting).id
            if resting is not None and resting < choices.option_count
            else None
        )
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


class SessionDetailScreen(ChoiceScreen):
    """One session's state, what it means, and the actions the policy allows on it."""

    #: always at least Back, plus whatever the policy allows.
    empty_state = NEVER_EMPTY

    def __init__(self, session_value: str) -> None:
        super().__init__()
        self.session_value = session_value
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
        self.show_choices(self.detail_entries(record, await self._observed_trust(record)))

    async def _observed_trust(self, record: SessionRecord) -> TrustState:
        """Read the pane's trust state. No session-state gate; see the bot's twin for why.

        In short: a trust-blocked `claude-remote` launch can land RUNNING, because its
        readiness marker can be observed before the dialog renders. State is not evidence
        about the dialog; only the pane is. Failures are swallowed to UNKNOWN rather than
        reported -- a pane we cannot read is one we must not offer to answer, and it is not
        worth replacing the detail with an error.
        """
        if not trust_available(record, TrustState.AWAITING):
            return TrustState.UNKNOWN
        read = getattr(self.services, "trust_state", None)
        if read is None:
            return TrustState.UNKNOWN
        try:
            return await read(record.session_id)
        except Exception:
            return TrustState.UNKNOWN

    def detail_entries(
        self, record: SessionRecord, trust: TrustState = TrustState.UNKNOWN
    ) -> tuple[tuple[str, str], ...]:
        """The actions this session offers, taken from the policy and not decided here.

        The stop entries are exactly `available_actions(record.state, record.orphan_provenance)`
        in the order it returns them, which puts force last. Adding, filtering, or reordering
        here is what `tests/contract/test_session_actions_parity.py` exists to catch.

        Provenance is passed rather than dropped because an ORPHANED record's rows depend on
        it (DEC-020), and a surface that passed only the state would silently render the
        conservative set — a divergence the parity contract cannot see if the other surface
        does the same thing.
        """
        entries: list[tuple[str, str]] = [("attach", "Copy attach")]
        if self.services.capture is not None:
            entries.append(("inspect", "Inspect output"))
        # Grouped with the read-only rows above rather than with the stops below, and the bot's
        # twin gives the reason in the same words: renaming changes what the session is called
        # and nothing about what it is doing. Offered in every state for the reason
        # `SessionService.rename` does not gate on one — naming a session that has just ended is
        # harmless, and the row it is on is still listed until reconciliation removes it.
        entries.append(("rename", "Rename"))
        if trust_available(record, trust):
            # Above the stop rows and below the read-only ones, because it is neither: it
            # unblocks a session rather than reading or ending it. Defaults to absent --
            # `trust` is UNKNOWN unless a caller went and looked, so a surface that forgets
            # to observe renders no row rather than a row that cannot work.
            entries.append(("trust", "Trust this project"))
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
        elif key == "trust":
            await self.answer_trust()
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

    async def answer_trust(self) -> None:
        """Answer the folder-trust question, re-reading the record and the pane first.

        Not modal-confirmed, and that is a judgment worth writing down rather than an
        omission. The two actions DEC-008 puts a confirmation in front of are force stop and
        Remote Control, and this one destroys nothing: it answers a question the agent is already
        asking, with the answer the owner would have to give at the keyboard for the session
        to be usable at all. A confirmation here would ask "are you sure you want to unblock
        the thing you launched".

        What it is guarded by instead is the pane itself. `answer_trust` on the terminal
        re-reads the capture and refuses unless the dialog is still on screen, so the worst
        a stale row can do is nothing.
        """
        async with self.holding_the_guard():
            record = await self.tui.current_record(self.session_value)
            if record is None:
                await self.refuse()
                return
            async with self.awaiting("Trusting the project…"):
                answered = await self.tui.answer_trust(record, self)
            if answered is None:
                return
            if answered is TrustState.AWAITING:
                self.set_status("The project is still waiting to be trusted. Try again.")
            else:
                self.set_status("Trusted. Relaunch the session if the agent already gave up.")
            await self.render_detail()

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
                command = await self.services.launcher.copy_attach(record.session_id)
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
        capture = self.services.capture
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
            text = render_capture(captured, self.services.capture_redactions)
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

        Awaited inline under the guard rather than run on a worker, matching `answer_trust`
        rather than `ProjectReviewScreen._create_project`. That screen went off the pump
        because creating a project scans the development root and writes a directory, so
        holding the pump made `ctrl+q` unanswerable for the duration. A rename is one indexed
        UPDATE under the session lock — the same cost class as the record read directly above
        it, which is already awaited here.

        **A repeat is refused by two checks together, and an earlier version of this paragraph
        credited the busy guard alone — which cannot do it here on its own.** `tui.busy` is
        consulted by
        `check_action` and by `on_option_list_option_selected`, so it drops a repeated *row*
        selection; nothing on the `Input.Submitted` dispatch path reads it, and `awaiting`
        covers `#choices` rather than the entry. This is the surface's only mutating submit, and
        it was written without the check both sibling committing entries have
        (`LabelScreen.submit`, `NameScreen.submit`).

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
                    await self.services.launcher.rename(record.session_id, label)
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


def render_capture(captured: str, redactions: tuple[str, ...]) -> str:
    """Turn a raw capture into what the output pane should show.

    `ports/terminal_text.sanitize_terminal_text` is the shared safety transformation, so
    nothing is re-implemented here. What is deliberately *not* reused is the Telegram
    presentation wrapper: its 4096-UTF-16-unit inline cap and session-output.txt attachment
    fallback exist because Telegram messages are bounded, and a scrollable local pane is not.
    """
    raw = captured.encode()
    if b"\x00" in raw:
        # Matching the bot's refusal, for the same reason: a pane emitting NUL is not
        # rendering text, and printing it to a terminal can corrupt the display.
        return "This session's output is binary and cannot be displayed."
    return sanitize_terminal_text(
        raw,
        max_lines=_INSPECT_MAX_LINES,
        max_bytes=_INSPECT_MAX_BYTES,
        redactions=redactions,
    )
