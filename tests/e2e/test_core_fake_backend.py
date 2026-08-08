"""End-to-end approved lifecycle against an in-memory fake backend."""

from collections.abc import Collection, Sequence
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
    SessionId,
    SessionRecord,
    SessionState,
)
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
        current = self.records[session_id]
        updated = SessionRecord(
            current.session_id,
            current.project_id,
            current.profile_id,
            current.display,
            transition(current.state, event).to_state,
            current.created_at,
        )
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
