"""In-memory terminal adapter for harmless end-to-end lifecycle tests."""

from __future__ import annotations

from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.ports.terminal import TerminalObservation


class FakeTerminal:
    """Model only the terminal observations the core application is allowed to use."""

    def __init__(self) -> None:
        self._observations: dict[SessionId, TerminalObservation] = {}

    async def launch(
        self, session_id: SessionId, project_id: ProjectId, profile_id: ProfileId
    ) -> TerminalObservation:
        """Create a live fake session without running a process."""
        del project_id, profile_id
        observation = TerminalObservation(session_id, live=True, preserved=False)
        self._observations[session_id] = observation
        return observation

    async def resume(
        self,
        session_id: SessionId,
        project_id: ProjectId,
        profile_id: ProfileId,
        source_id: ProviderConversationId,
    ) -> TerminalObservation:
        """Model a trusted provider selection without accepting Telegram arguments."""
        del source_id
        return await self.launch(session_id, project_id, profile_id)

    async def inspect(self, session_id: SessionId) -> TerminalObservation | None:
        """Return the current fake terminal observation, if it remains managed."""
        return self._observations.get(session_id)

    async def graceful_stop(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation:
        """End the fake process while retaining its inspectable session."""
        del profile_id
        observation = TerminalObservation(session_id, live=False, preserved=True)
        self._observations[session_id] = observation
        return observation

    async def cleanup(self, session_id: SessionId) -> None:
        """Remove a preserved fake session."""
        self._observations.pop(session_id, None)

    async def force_stop(self, session_id: SessionId) -> TerminalObservation:
        """Remove a live fake session and report its verified termination."""
        self._observations.pop(session_id, None)
        return TerminalObservation(session_id, live=False, preserved=False)
