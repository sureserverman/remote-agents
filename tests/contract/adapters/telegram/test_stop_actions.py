from __future__ import annotations

from uuid import UUID

import pytest

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.stops import StopController
from remote_agents.domain.models import ProfileId, SessionId, SessionState


class FakeService:
    def __init__(self) -> None:
        self.actions: list[str] = []

    async def graceful_stop(self, _command) -> None:
        self.actions.append("graceful")

    async def cleanup(self, _command) -> None:
        self.actions.append("cleanup")

    async def force_stop(self, _command) -> None:
        self.actions.append("force")


class Record:
    def __init__(self, session_id: SessionId, state: SessionState) -> None:
        self.session_id = session_id
        self.state = state


def test_stop_actions_require_the_right_confirmation_strength_and_claim_once() -> None:
    controller = StopController(CallbackStateStore())
    session = SessionId(UUID(int=1))
    graceful = controller.offer(
        session, ProfileId("claude"), SessionState.RUNNING, "graceful", 7, 11, 1
    )
    force = controller.offer(session, ProfileId("claude"), SessionState.RUNNING, "force", 7, 11, 1)

    claimed = controller.claim(graceful, 7, 11, 1)
    assert claimed is not None
    assert (claimed.action, claimed.session_id, claimed.profile_id) == (
        "graceful",
        session,
        ProfileId("claude"),
    )
    assert controller.claim(graceful, 7, 11, 1) is None
    assert controller.claim(force, 7, 11, 1) is None
    assert controller.confirm_force(force, 7, 11, 1)
    claimed_force = controller.claim(force, 7, 11, 1)
    assert claimed_force is not None and claimed_force.action == "force"


def test_cleanup_only_exists_for_preserved_sessions() -> None:
    controller = StopController(CallbackStateStore())
    session = SessionId(UUID(int=1))

    assert (
        controller.offer(session, ProfileId("claude"), SessionState.RUNNING, "cleanup", 7, 11, 1)
        is None
    )
    assert controller.offer(
        session, ProfileId("claude"), SessionState.PRESERVED, "cleanup", 7, 11, 1
    )


@pytest.mark.asyncio
async def test_claimed_action_rechecks_current_state_before_typed_service_dispatch() -> None:
    controller = StopController(CallbackStateStore())
    session = SessionId(UUID(int=1))
    token = controller.offer(
        session, ProfileId("claude"), SessionState.RUNNING, "graceful", 7, 11, 1
    )
    assert token is not None
    claimed = controller.claim(token, 7, 11, 1)
    assert claimed is not None
    service = FakeService()

    assert not await controller.execute(claimed, service, Record(session, SessionState.PRESERVED))
    assert service.actions == []
