"""Listing managed sessions, one session's detail, and its captured output.

Three screens replacing three `Step` members. `_detail_id` — one of the seven navigation
fields — is `SessionDetailScreen.session_value` here, so the detail cannot be rendered for a
session the screen was not opened with, and no other flow can leave a stale id behind for it
to read.

`SessionDetailScreen` still carries a little of the step machine, and only for the two
confirmation positions Stage 3 replaces with real modals. That is deliberate and bounded:
those confirmations are what DEC-007 rests on, the plan gives them their own stage, and
repainting them onto this screen is exactly what the surface does today — so nothing about
the confirmation path changes here. Stage 3 deletes that coupling along with the two `Step`
members it reads.
"""

from __future__ import annotations

import logging

from remote_agents.adapters.tui.model import _BACK, session_row
from remote_agents.adapters.tui.screens.base import ChoiceScreen
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

    async def on_screen_resume(self) -> None:
        """Re-read readiness, then list what the shared store actually holds.

        On `ScreenResume` rather than on mount, because this fires both when the screen is
        pushed and when it becomes active again after the detail above it is popped — which
        is what preserves the behaviour the hand-rolled chain had, where Back from a detail
        re-entered the list through a fresh read. A pop alone would have left the owner
        looking at a list built before whatever they had just done to it.

        Readiness is refreshed first for the same reason the bot does it: a launch that
        failed here may have become ready since, and listing a stale FAILED would send the
        owner to fix something that already works.
        """
        await self.reload()

    async def reload(self) -> None:
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

    @property
    def position(self) -> str:  # type: ignore[override]
        """Which of the three positions this screen is currently showing.

        A property rather than the plain class attribute the other screens use, because until
        Stage 3 this one screen hosts the two confirmation positions as well as the detail.
        Reporting a fixed "SESSION_DETAIL" would make the committed baselines for the two
        confirms compare against a position the test never actually reached.
        """
        return self.tui.step.name

    async def populate(self) -> None:
        self.hide_entry()

    async def on_screen_resume(self) -> None:
        """Draw the detail on entry, and re-draw it on the way back from Inspect.

        On `ScreenResume` rather than in `populate` for the same reason `SessionsScreen`
        loads there: it fires both on push and when the screen above is popped. The chain
        this replaces re-ran the whole detail when Escape left Inspect, so a session whose
        state moved while the owner was reading its output came back to a refreshed screen.
        A bare pop would have returned them to the render from before they left.
        """
        await self.render_detail()

    async def render_detail(self) -> None:
        """Show the session's state, re-read from the shared store.

        The record is looked up again rather than trusted from the list: the store has two
        writers, so a session can be stopped elsewhere while this list is on screen.
        """
        from remote_agents.adapters.tui.app import Step

        tui = self.tui
        tui.step = Step.SESSION_DETAIL
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
        """Act on a row, for whichever of the three positions is showing.

        The `Step` read is the transitional coupling this module's docstring names: the two
        confirmations are still repainted onto this screen rather than pushed as modals, so
        which resolver owns a row still depends on which of them is showing. Stage 3 removes
        both branches with the members they read.
        """
        from remote_agents.adapters.tui.app import Step

        tui = self.tui
        if tui.step is Step.FORCE_CONFIRM:
            await tui.resolve_force_confirm(key)
            return
        if tui.step is Step.REMOTE_CONTROL_CONFIRM:
            await tui.resolve_remote_control(key)
            return
        await self.resolve_detail(key)

    async def resolve_detail(self, key: str) -> None:
        if key == _BACK:
            self.app.pop_screen()
        elif key == "attach":
            await self.show_attach()
        elif key == "inspect":
            await self.show_inspect()
        elif key == "remote-control":
            await self.tui.confirm_remote_control()
        elif key == FORCE:
            await self.tui.confirm_force()
        elif key in ACTION_LABELS and key != FORCE:
            # The `key != FORCE` is redundant with the branch above and deliberately kept:
            # FORCE is a member of ACTION_LABELS, so without it the only thing stopping a
            # single keypress from force-stopping is the *order* of these two branches.
            # Restructuring this chain into a dispatch table would silently remove the
            # confirmation step, and no existing test asserts the ordering itself.
            await self.tui.stop(key)

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
