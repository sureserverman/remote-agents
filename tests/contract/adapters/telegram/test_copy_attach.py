"""Copy Attach is offered only for current trusted managed-pane evidence."""

from __future__ import annotations

from datetime import UTC, datetime

from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.adapters.tmux.codec import attach_command
from remote_agents.application.commands import InspectQuery
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.terminal import TerminalObservation


class Launcher:
    def __init__(self, record: SessionRecord, observation: TerminalObservation | None) -> None:
        self.record = record
        self.observation = observation

    async def list_sessions(self):
        return (self.record,)

    async def inspect(self, query: InspectQuery):
        assert query.session_id == self.record.session_id
        return self.observation

    async def copy_attach(self, session_id: SessionId) -> str | None:
        return (
            attach_command(session_id)
            if self.observation is not None and self.observation.live
            else None
        )


async def test_copy_attach_requires_live_matching_project_and_profile_evidence() -> None:
    session_id = SessionId.new()
    project_id = ProjectId("a" * 24)
    record = SessionRecord(
        session_id,
        project_id,
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )
    boundary = PrivateBotBoundary(
        7,
        11,
        catalogue=(CatalogProject(str(project_id), "opaque-editor", "writing", "Registered"),),
        launcher=Launcher(
            record,
            TerminalObservation(
                session_id, True, False, project_id=project_id, profile_id=ProfileId("claude")
            ),
        ),
    )
    await boundary._home_reply()

    detail = await boundary._detail_reply(str(session_id))
    attach = next(
        button for row in detail.keyboard for button in row if button.text == "Copy attach"
    )
    response = await boundary._attach_reply(str(session_id))

    assert attach.callback_data.startswith("c1_")
    assert f"ra-{session_id}:" in response.text
    assert "tmux -L remote-agents attach-session" in response.text


async def test_copy_attach_is_hidden_when_the_pane_is_not_currently_live() -> None:
    session_id = SessionId.new()
    project_id = ProjectId("a" * 24)
    record = SessionRecord(
        session_id,
        project_id,
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )
    boundary = PrivateBotBoundary(7, 11, launcher=Launcher(record, None))
    await boundary._home_reply()

    detail = await boundary._detail_reply(str(session_id))

    assert "Copy attach" not in [button.text for row in detail.keyboard for button in row]
