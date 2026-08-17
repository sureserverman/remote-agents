from datetime import UTC, datetime

from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import RemoteControlState


class Launcher:
    def __init__(self, record: SessionRecord) -> None:
        self.record = record
        self.commands = []

    async def list_sessions(self):
        return (self.record,)

    async def inspect(self, _query):
        return None

    async def set_remote_control(self, command):
        self.commands.append(command)
        return command.desired_state


async def test_claude_remote_control_requires_confirmation_and_uses_opaque_callbacks() -> None:
    record = SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )
    launcher = Launcher(record)
    boundary = PrivateBotBoundary(7, 11, launcher=launcher)
    detail = await boundary._detail_reply(str(record.session_id))
    token = next(
        button.callback_data
        for row in detail.keyboard
        for button in row
        if button.text == "Enable Remote Control"
    )
    boundary.callbacks.bind_pending(11, 1)
    state = boundary.callbacks.resolve(token, owner_id=7, chat_id=11, message_id=1)
    assert state is not None and state.action == "remote.control"
    confirmation = await boundary._remote_control_confirm_reply(state.entity_id)
    boundary.callbacks.bind_pending(11, 1)
    result = await boundary._remote_control_reply(
        state.entity_id, confirmation.keyboard[0][0].callback_data, 1
    )

    assert "Remote Control: active" in result["text"]
    assert launcher.commands[0].desired_state is RemoteControlState.ACTIVE
