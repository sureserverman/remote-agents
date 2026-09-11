"""The bot's single Remote Control button, driven through a real use case and a fake pane.

The contract test beside this one models the use case; this one wires the real
`SessionService` to `FakeTerminal`, so the reading the confirmation names has actually
travelled the whole path a press takes -- bot, use case, port, pane -- rather than being
handed to the screen by a double that agreed with it in advance.
"""

from datetime import UTC, datetime

import pytest
from backends import backend_for

from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.adapters.tmux.runtime import _remote_control_state
from remote_agents.application.services import SessionService
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import RemoteControlState


def test_remote_control_fake_backend_fails_closed_for_unclassified_capture():
    assert _remote_control_state("unclassified") is RemoteControlState.UNKNOWN


class _Store:
    """Just enough store for the reads this flow makes, and nothing it does not."""

    def __init__(self, record: SessionRecord) -> None:
        self.record = record
        self._claimed: set[str] = set()
        self.written: list[RemoteControlState] = []

    async def get(self, session_id):
        return self.record if session_id == self.record.session_id else None

    async def list_active(self):
        return (self.record,)

    async def claim_idempotency_key(self, key):
        if key in self._claimed:
            return False
        self._claimed.add(key)
        return True

    async def set_remote_control_state(self, session_id, state):
        del session_id
        self.written.append(state)
        return self.record


def _wired(reading: RemoteControlState):
    record = SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )
    terminal = FakeTerminal()
    terminal.remote_control_states.append(reading)
    store = _Store(record)

    class _Service(SessionService):
        """The detail screen lists sessions; everything else is the real service."""

        async def list_sessions(self):
            return (record,)

    return record, store, terminal, _Service(store, terminal)


@pytest.mark.parametrize(
    ("reading", "carried"),
    [
        (RemoteControlState.ACTIVE, RemoteControlState.INACTIVE),
        (RemoteControlState.INACTIVE, RemoteControlState.ACTIVE),
        (RemoteControlState.UNKNOWN, RemoteControlState.ACTIVE),
    ],
)
async def test_one_press_carries_the_direction_the_real_pane_read_implies(
    reading: RemoteControlState, carried: RemoteControlState
) -> None:
    record, store, terminal, service = _wired(reading)
    boundary = build_private_bot(7, 11, backend=backend_for(sessions=service))

    detail = await boundary._detail_reply(str(record.session_id))
    token = next(
        button.callback_data
        for row in detail.keyboard
        for button in row
        if button.text.endswith("Remote Control")
    )
    boundary.callbacks.bind_pending(11, 1)
    pressed = boundary.callbacks.resolve(token, owner_id=7, chat_id=11, message_id=1)
    assert pressed is not None
    confirmation = await boundary._remote_control_confirm_reply(pressed.entity_id)
    boundary.callbacks.bind_pending(11, 1)
    mutation = boundary.callbacks.resolve(
        confirmation.keyboard[0][0].callback_data, owner_id=7, chat_id=11, message_id=1
    )
    assert mutation is not None

    assert mutation.entity_id.endswith(f"|{carried.value}")
    assert terminal.remote_control_states == [], "the reading was consumed by the confirmation"
