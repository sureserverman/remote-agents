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

from remote_agents.adapters.tui.model import _BACK, session_row
from remote_agents.adapters.tui.screens.base import ChoiceScreen
from remote_agents.adapters.tui.screens.confirm import (
    ForceConfirmModal,
    RemoteControlConfirmModal,
)
from remote_agents.application.session_actions import (
    ACTION_LABELS,
    FORCE,
    available_actions,
    explain_state,
    remote_control_available,
)
from remote_agents.domain.models import SessionRecord
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.ports.terminal_text import sanitize_terminal_text

_LOG = logging.getLogger(__name__)

_INSPECT_MAX_LINES = 2000
_INSPECT_MAX_BYTES = 512 * 1024

#: The two Remote Control directions: the row key, what the row says, and the state that row
#: asks for. One table rather than a list of rows beside a lookup, so a row cannot come to
#: exist without a direction behind it — which is the defect the single-row version had, where
#: the direction was decided a screen later by whichever of two buttons was pressed.
_REMOTE_CONTROL_ROWS: tuple[tuple[str, str, RemoteControlState], ...] = (
    ("remote-control-active", "Enable Remote Control", RemoteControlState.ACTIVE),
    ("remote-control-inactive", "Disable Remote Control", RemoteControlState.INACTIVE),
)
_REMOTE_CONTROL_DIRECTIONS = {key: state for key, _label, state in _REMOTE_CONTROL_ROWS}


class SessionsScreen(ChoiceScreen):
    """Every managed session, including ones this process never launched."""

    position = "SESSIONS"

    async def populate(self) -> None:
        self.hide_entry()
        await self.reload()

    async def on_reveal(self) -> None:
        """Re-read on the way back from a detail, as the hand-rolled chain did."""
        await self.reload()

    async def reload(self) -> None:
        """Refresh readiness, then list what the shared store actually holds.

        Readiness is refreshed first for the same reason the bot does it: a launch that
        failed here may have become ready since, and listing a stale FAILED would send the
        owner to fix something that already works.
        """
        try:
            records = await self.tui.load_sessions()
        except Exception as error:
            self.tui.report_store_failure(error)
            return
        if not records:
            self.show_choices(())
            self.set_status(
                "There are no managed sessions. Press escape to return to the project list."
            )
            return
        self.set_status(f"{len(records)} managed session(s). Select one for detail.")
        self.show_choices(
            tuple((str(record.session_id), session_row(record)) for record in records)
        )

    async def choose(self, key: str) -> None:
        await self.tui.show_detail(key)


class SessionDetailScreen(ChoiceScreen):
    """One session's state, what it means, and the actions the policy allows on it."""

    def __init__(self, session_value: str) -> None:
        super().__init__()
        self.session_value = session_value

    position = "SESSION_DETAIL"

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
            tui.report_store_failure(error)
            return
        if record is None:
            self.show_choices(((_BACK, "Back"),))
            self.set_status("That session is no longer available.")
            return
        self.set_status(
            f"{record.display.rendered}\nState: {record.state.value}\n{explain_state(record.state)}"
        )
        self.show_choices(self.detail_entries(record))

    def detail_entries(self, record: SessionRecord) -> tuple[tuple[str, str], ...]:
        """The actions this session offers, taken from the policy and not decided here.

        The stop entries are exactly `available_actions(record.state)` in the order it
        returns them, which puts the destructive one last. Adding, filtering, or reordering
        here is what `tests/contract/test_session_actions_parity.py` exists to catch.
        """
        entries: list[tuple[str, str]] = [("attach", "Copy attach")]
        if self.services.capture is not None:
            entries.append(("inspect", "Inspect output"))
        if remote_control_available(record):
            # One row per direction, so the decision is taken here and the confirmation that
            # follows has exactly one thing to confirm. The single "Claude Remote Control"
            # row this replaces opened a three-row screen where Enable and Disable sat
            # side by side under a heading — a chooser wearing a confirmation's clothes, and
            # the reason that step could not be answered with a yes or a no. It also puts the
            # surface back in step with the bot, which has offered these two buttons since
            # the feature landed.
            entries.extend((key, label) for key, label, _state in _REMOTE_CONTROL_ROWS)
        entries.extend(
            (action, ACTION_LABELS[action]) for action in available_actions(record.state)
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
        outright when it is already held.
        """
        async with self.holding_the_guard():
            record = await self.tui.current_record(self.session_value)
            if record is None:
                self.set_status("That session is no longer available.")
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
                self.set_status(
                    f"{record.display.rendered}\n"
                    f"The confirmation could not be shown: {error}\n"
                    "Nothing was stopped."
                )
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

        Guarded, answered and released for the reasons given on `confirm_force`, which this
        deliberately mirrors line for line: two destructive-ish confirmations that differ in
        their control flow are two things to get right rather than one.

        The policy is re-checked here *and* again inside `set_remote_control`. That is not
        redundant — this check decides whether to ask at all, and that one decides whether to
        act on the answer, with the modal's whole open duration in between.
        """
        async with self.holding_the_guard():
            record = await self.tui.current_record(self.session_value)
            if record is None:
                self.set_status("That session is no longer available.")
                return
            if not remote_control_available(record):
                self.set_status(
                    f"{record.display.rendered}\n"
                    "Remote Control is not available for this session.\n"
                    f"{explain_state(record.state)}"
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
                self.set_status(
                    f"{record.display.rendered}\n"
                    f"The confirmation could not be shown: {error}\n"
                    "Nothing was changed."
                )
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
                    self.set_status("That session is no longer available.")
                    return
                command = await self.services.launcher.copy_attach(record.session_id)
            except Exception as error:
                self.tui.report_store_failure(error)
                return
        if command is None:
            self.set_status(
                f"{record.display.rendered}\n"
                "Attach is not available: this session's pane is not live, or the pane "
                "found for it belongs to a different project or agent.\n"
                f"{explain_state(record.state)}"
            )
            return
        self.set_status(f"{record.display.rendered}\nAttach with:\n{command}")

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
                self.set_status("That session is no longer available.")
                return
            try:
                captured = await capture(record.session_id)
            except Exception as error:
                _LOG.exception("capture failed")
                self.set_status(
                    f"{record.display.rendered}\nThe output could not be captured: {error}"
                )
                return
            text = render_capture(captured, self.services.capture_redactions)
            await self.advance_to(
                InspectScreen(
                    f"{record.display.rendered}\nOutput. Press escape to go back.",
                    text or "This session has produced no output yet.",
                )
            )


class InspectScreen(ChoiceScreen):
    """This session's captured output, on the scrollable pane rather than in the list."""

    position = "INSPECT"

    def __init__(self, status: str, output: str) -> None:
        super().__init__()
        self._status_text = status
        self._output_text = output

    async def populate(self) -> None:
        self.hide_entry()
        self.show_choices(())
        self.set_status(self._status_text)
        self.show_output(self._output_text)


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
