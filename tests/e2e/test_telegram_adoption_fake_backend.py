"""Fake owner transport exposes external local sessions as read-only evidence."""

from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.domain.external_sessions import (
    ExternalSessionReference,
    ExternalSessionState,
    ExternalSessionSummary,
)
from remote_agents.domain.models import ProfileId, ProjectId


class Launcher:
    async def list_sessions(self):
        return ()

    async def list_external_sessions(self):
        return (
            ExternalSessionSummary(
                ExternalSessionReference("p-0123456789abcdef"),
                ProfileId("claude"),
                ProjectId("opaque-editor"),
                ExternalSessionState.RUNNING_EXTERNALLY,
            ),
        )


async def test_telegram_adoption_fake_backend_shows_running_external_state() -> None:
    boundary = PrivateBotBoundary(7, 11, launcher=Launcher())

    reply = await boundary._local_sessions_reply()

    assert "Local Sessions" in reply.text
    assert reply.keyboard[0][0].text == "Claude · running_externally"
