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

    async def set_remote_control_state(self, session_id, state):
        self.observed = (session_id, state)
        return self.record


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


async def test_the_state_read_is_a_pass_through_and_takes_no_operation_lock():
    """Reading the pane must not queue behind a stop the owner is trying to issue.

    The same shape as `trust_state`: a question about a pane *right now*, answered without
    the operation lock, because a confirmation screen rendering a reading is not a mutation
    and must not be able to block one. The direction the confirmation names comes from here,
    so a read that could block would make the single toggle slower than the pair it replaced.
    """
    record = SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )
    terminal = FakeTerminal()
    terminal.remote_control_states.append(RemoteControlState.ACTIVE)
    service = SessionService(Store(record), terminal)

    async with service._locks.operation():
        assert await service.remote_control_state(record.session_id) is RemoteControlState.ACTIVE


async def test_an_unarmed_pane_reads_unknown_rather_than_inventing_a_direction():
    """A fake that answered ACTIVE by default would name a direction in every test that
    never thought about Remote Control — the defaulting mistake `trust_state` avoids."""
    record = SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )
    service = SessionService(Store(record), FakeTerminal())

    assert await service.remote_control_state(record.session_id) is RemoteControlState.UNKNOWN


async def test_a_refusal_keeps_the_stored_reading():
    """BL-109: a refused press left the pane as it was, so the record keeps what it said.

    Recording UNKNOWN here is what used to clear a stored ACTIVE on a press that typed nothing.
    The refusal reaches the caller with its reason instead, for the surface to say why.
    """
    from dataclasses import replace

    from remote_agents.ports.terminal import RemoteControlRefusal, RemoteControlRefused

    record = SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )
    store = Store(replace(record, remote_control_state=RemoteControlState.ACTIVE))
    terminal = FakeTerminal()
    terminal.remote_control_refusal = RemoteControlRefusal.BUSY
    service = SessionService(store, terminal)
    command = RemoteControlCommand(record.session_id, RemoteControlState.INACTIVE, "remote-1")

    with pytest.raises(RemoteControlRefused) as refused:
        await service.set_remote_control(command)

    assert refused.value.reason is RemoteControlRefusal.BUSY
    assert not hasattr(store, "observed"), "set_remote_control_state was called on a refusal"
    assert store.record.remote_control_state is RemoteControlState.ACTIVE
