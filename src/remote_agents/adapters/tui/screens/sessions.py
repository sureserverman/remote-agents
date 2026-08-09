"""Listing managed sessions, one session's detail, and its captured output.

Three screens replacing three wizard positions. The session id the detail renders was one of
the seven navigation fields the app used to carry; it is `SessionDetailScreen.session_value`
here, so the detail cannot be rendered for a session the screen was not opened with, and no
other flow can leave a stale id behind for it to read.

The two destructive confirmations are no longer repainted onto this screen: they are
`screens/confirm.py`, pushed and popped like any other position, which is what let the step
machine be deleted. They are still ordinary `Screen`s — Stage 3 turns them into
`ModalScreen[bool]` answered through `push_screen_wait`, which is what buys DEC-007 the
guarantee that an app-level binding cannot walk away from an unanswered confirmation.
"""

from __future__ import annotations

import logging

from remote_agents.adapters.tui.model import _BACK, session_row
from remote_agents.adapters.tui.screens.base import ChoiceScreen
from remote_agents.adapters.tui.screens.confirm import (
    ForceConfirmScreen,
    RemoteControlConfirmScreen,
)
from remote_agents.application.session_actions import (
    ACTION_LABELS,
    FORCE,
    available_actions,
    explain_state,
    remote_control_available,
)
from remote_agents.domain.models import SessionRecord
from remote_agents.ports.terminal_text import sanitize_terminal_text

_LOG = logging.getLogger(__name__)

_INSPECT_MAX_LINES = 2000
_INSPECT_MAX_BYTES = 512 * 1024


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
            entries.append(("remote-control", "Claude Remote Control"))
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
        elif key == "remote-control":
            await self.confirm_remote_control()
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
        """Re-read the record, then hand the decision to its own screen.

        Guarded across both the read and the push, and this guard is load-bearing twice over.
        `action_back` runs on the app's pump while this runs on the screen's, so without it an
        Escape landing inside the read pops *this* screen — and then the confirmation is
        pushed onto whatever the pop revealed, describing a session the position beneath it is
        no longer showing. Worse, the `set_status` below would be called on a screen that has
        already been unmounted, raising `NoMatches` out of the very path that exists to report
        a vanished session without losing the app.
        """
        tui = self.tui
        tui.set_busy(True)
        try:
            record = await tui.current_record(self.session_value)
            if record is None:
                self.set_status("That session is no longer available.")
                return
            await self.app.push_screen(ForceConfirmScreen(self.session_value, record))
        finally:
            tui.set_busy(False)

    async def confirm_remote_control(self) -> None:
        """Ask before changing a live pane's control mode, re-checking the policy first.

        Guarded for the reason given on `confirm_force`.
        """
        tui = self.tui
        tui.set_busy(True)
        try:
            record = await tui.current_record(self.session_value)
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
            await self.app.push_screen(RemoteControlConfirmScreen(self.session_value, record))
        finally:
            tui.set_busy(False)

    async def show_attach(self) -> None:
        """Render the command that reaches this pane, or say why there is none.

        The affordance is always offered and answers when chosen, rather than being hidden
        when unavailable. Hiding it is what the bot does, and it leaves the owner unable to
        tell a dead pane from a surface that simply forgot to draw the button.
        """
        tui = self.tui
        try:
            record = await tui.current_record(self.session_value)
            if record is None:
                self.set_status("That session is no longer available.")
                return
            command = await self.services.launcher.copy_attach(record.session_id)
        except Exception as error:
            tui.report_store_failure(error)
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
        record = await self.tui.current_record(self.session_value)
        if record is None:
            self.set_status("That session is no longer available.")
            return
        try:
            captured = await capture(record.session_id)
        except Exception as error:
            _LOG.exception("capture failed")
            self.set_status(f"{record.display.rendered}\nThe output could not be captured: {error}")
            return
        text = render_capture(captured, self.services.capture_redactions)
        await self.app.push_screen(
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
