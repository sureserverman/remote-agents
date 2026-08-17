"""End-to-end approved lifecycle against an in-memory fake backend."""

from collections.abc import Collection, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from remote_agents.adapters.projects.discovery import DiscoveredProject
from remote_agents.adapters.projects.registry import RegisteredProject
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.application.commands import (
    GracefulStopCommand,
    InspectQuery,
    LaunchCommand,
)
from remote_agents.application.project_catalog import build_catalogue
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
from remote_agents.domain.state_machine import LifecycleEvent, transition


class InMemorySessionStore:
    """Minimal durable-port double for a complete harmless lifecycle."""

    def __init__(self) -> None:
        self.records: dict[SessionId, SessionRecord] = {}
        self.claimed_keys: set[str] = set()

    async def next_sequence(self, project_id: ProjectId, profile_id: ProfileId) -> int:
        return 1 + sum(
            record.project_id == project_id and record.profile_id == profile_id
            for record in self.records.values()
        )

    async def save(self, record: SessionRecord) -> None:
        self.records[record.session_id] = record

    async def get(self, session_id: SessionId) -> SessionRecord | None:
        return self.records.get(session_id)

    async def list(self, states: Collection[SessionState] | None = None) -> Sequence[SessionRecord]:
        records = tuple(self.records.values())
        return (
            records
            if states is None
            else tuple(record for record in records if record.state in states)
        )

    async def record_event(self, session_id: SessionId, event: LifecycleEvent) -> SessionRecord:
        # `replace` rather than a positional rebuild — see the twin in test_reconcile.py: the
        # old shape dropped every field after created_at, so a fake could lose what the real
        # store carries and the test would still pass.
        current = self.records[session_id]
        updated = replace(current, state=transition(current.state, event).to_state)
        self.records[session_id] = updated
        return updated

    async def set_label(self, session_id: SessionId, label: str | None) -> SessionRecord:
        current = self.records[session_id]
        updated = replace(
            current,
            display=SessionDisplayIdentity(
                current.display.project_slug,
                current.display.agent_label,
                current.display.mode,
                current.display.sequence,
                label,
            ),
        )
        self.records[session_id] = updated
        return updated

    async def set_remote_control_state(
        self, session_id: SessionId, state: RemoteControlState
    ) -> SessionRecord:
        """`replace`, and clearing on UNKNOWN, for the same two reasons the real store does.

        This double had already drifted from the port before Remote Control state existed --
        `set_label` was missing too -- and drift is invisible until some later test drives the
        method that is not there. Both are implemented now so the fake answers the whole port.
        """
        current = self.records[session_id]
        stored = None if state is RemoteControlState.UNKNOWN else state
        updated = replace(current, remote_control_state=stored)
        self.records[session_id] = updated
        return updated

    async def claim_idempotency_key(self, key: str) -> bool:
        if key in self.claimed_keys:
            return False
        self.claimed_keys.add(key)
        return True


@pytest.mark.asyncio
async def test_core_fake_lifecycle(tmp_path: Path) -> None:
    """Drive catalogue through launch and a graceful stop that ends the session itself."""
    project_path = tmp_path / "writing" / "opaque-editor"
    project_path.mkdir(parents=True)
    catalogue = build_catalogue(
        (RegisteredProject(project_path, "opaque-editor", "writing"),),
        (DiscoveredProject(project_path, "opaque-editor", "writing"),),
    )
    assert [(project.name, project.group) for project in catalogue] == [
        ("opaque-editor", "Registered")
    ]

    store = InMemorySessionStore()
    terminal = FakeTerminal()
    service = SessionService(store, terminal)
    project_id = ProjectId(catalogue[0].name)
    profile_id = ProfileId("claude")

    launched = await service.launch(LaunchCommand(project_id, profile_id, "launch-1"))
    assert launched.state is SessionState.RUNNING
    assert await service.list_sessions() == (launched,)
    assert (await service.inspect(InspectQuery(launched.session_id))).live is True

    stopped = await service.graceful_stop(GracefulStopCommand(launched.session_id, profile_id))
    assert stopped.preserved is True, "the observation still reports how the pane exited"
    assert (await store.get(launched.session_id)).state is SessionState.ENDED
    assert await service.inspect(InspectQuery(launched.session_id)) is None
