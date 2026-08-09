"""The two destructive confirmations, as screens of their own.

These were the last two `Step` members, repainted onto the session detail. Giving them a
screen each is what lets `Step` be deleted — and it is deliberately only *that*. They are
ordinary `Screen`s here, pushed and popped like every other position; Stage 3 turns them into
`ModalScreen[bool]` answered through `push_screen_wait`, which is what buys DEC-007's real
guarantee that an app-level binding cannot walk away from an unanswered confirmation.

What has not changed, and must not: the abort entry is **first**, so it is the row the cursor
rests on and the row a stray enter activates. Confirming means deliberately moving to a
different row. That is DEC-007's mitigation and it predates this refactor.
"""

from __future__ import annotations

from remote_agents.adapters.tui.model import _CANCEL
from remote_agents.adapters.tui.screens.base import ChoiceScreen
from remote_agents.application.session_actions import FORCE, explain_state
from remote_agents.domain.models import SessionRecord
from remote_agents.domain.remote_control import RemoteControlState

_ENABLE = "remote-control-active"
_DISABLE = "remote-control-inactive"


class ForceConfirmScreen(ChoiceScreen):
    """Ask a second time before killing a running agent.

    Force cannot be undone, so it is deliberately not reachable by repeating whatever
    keystroke opened the detail: the abort entry is first and highlighted, and confirming
    means moving to a different row on purpose.
    """

    position = "FORCE_CONFIRM"

    def __init__(self, session_value: str, record: SessionRecord) -> None:
        super().__init__()
        self.session_value = session_value
        self.record = record

    async def populate(self) -> None:
        self.hide_entry()
        self.set_status(
            f"Force stop {self.record.display.rendered}?\n"
            "This kills the agent immediately and cannot be undone. Any work it has not "
            "saved is lost.\n"
            f"{explain_state(self.record.state)}"
        )
        self.show_choices(((_CANCEL, "Cancel"), ("force-confirm", "Yes, force stop it")))

    async def after_command(self) -> None:
        """Leave the confirmation; the detail beneath re-reads on the way back."""
        await self.tui.go_back()

    async def choose(self, key: str) -> None:
        if key != "force-confirm":
            # Anything else -- cancel, back, or an unrecognized key -- aborts without issuing.
            await self.tui.go_back()
            return
        await self.tui.stop(FORCE, self.session_value, self)


class RemoteControlConfirmScreen(ChoiceScreen):
    """Ask before changing a live pane's control mode, with Cancel as the resting row."""

    position = "REMOTE_CONTROL_CONFIRM"

    def __init__(self, session_value: str, record: SessionRecord) -> None:
        super().__init__()
        self.session_value = session_value
        self.record = record

    async def populate(self) -> None:
        self.hide_entry()
        self.set_status(
            f"Claude Remote Control for {self.record.display.rendered}\n"
            "Enabling lets this session be driven remotely; disabling returns it to local "
            "control only."
        )
        self.show_choices(
            (
                (_CANCEL, "Cancel"),
                (_ENABLE, "Enable Remote Control"),
                (_DISABLE, "Disable Remote Control"),
            )
        )

    async def after_command(self) -> None:
        """Leave the confirmation; the detail beneath re-reads on the way back."""
        await self.tui.go_back()

    async def choose(self, key: str) -> None:
        desired = {_ENABLE: RemoteControlState.ACTIVE, _DISABLE: RemoteControlState.INACTIVE}.get(
            key
        )
        if desired is None:
            await self.tui.go_back()
            return
        await self.tui.set_remote_control(self.session_value, desired, self)
