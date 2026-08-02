from datetime import UTC, datetime

import pytest

from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.application.commands import RemoteControlCommand
from remote_agents.application.errors import DuplicateCommandError
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


class Store:
    def __init__(self, record) -> None:
        self.record = record
        self.claims = set()

    async def get(self, session_id):
        return self.record if session_id == self.record.session_id else None

    async def claim_idempotency_key(self, key):
        if key in self.claims:
            return False
        self.claims.add(key)
        return True


async def test_remote_control_is_typed_profile_limited_and_idempotent():
    record = SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )
    service = SessionService(Store(record), FakeTerminal())
    command = RemoteControlCommand(record.session_id, RemoteControlState.ACTIVE, "remote-1")
    assert await service.set_remote_control(command) is RemoteControlState.UNKNOWN
    with pytest.raises(DuplicateCommandError):
        await service.set_remote_control(command)
