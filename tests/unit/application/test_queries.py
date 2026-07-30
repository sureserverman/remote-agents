"""Query tests keep terminal liveness separate from durable session metadata."""

from remote_agents.application.commands import InspectQuery
from remote_agents.application.services import SessionService
from remote_agents.domain.models import SessionId
from remote_agents.ports.terminal import TerminalObservation


class FakeStore:
    async def get(self, session_id: SessionId) -> None:
        return None


class FakeTerminal:
    async def inspect(self, session_id: SessionId) -> TerminalObservation:
        return TerminalObservation(session_id, live=True, preserved=False)


async def test_inspect_returns_terminal_observation_not_store_state() -> None:
    session_id = SessionId.new()
    service = SessionService(FakeStore(), FakeTerminal())

    observation = await service.inspect(InspectQuery(session_id))

    assert observation is not None
    assert observation.live is True
