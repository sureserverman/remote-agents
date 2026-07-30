from __future__ import annotations

from uuid import UUID

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.stops import StopController
from remote_agents.domain.models import ProfileId, SessionId, SessionState


def test_stop_actions_require_the_right_confirmation_strength_and_claim_once() -> None:
    controller = StopController(CallbackStateStore())
    session = SessionId(UUID(int=1))
    graceful = controller.offer(
        session, ProfileId("claude"), SessionState.RUNNING, "graceful", 7, 11, 1
    )
    force = controller.offer(session, ProfileId("claude"), SessionState.RUNNING, "force", 7, 11, 1)

    assert controller.claim(graceful, 7, 11, 1) == "graceful"
    assert controller.claim(graceful, 7, 11, 1) is None
    assert controller.claim(force, 7, 11, 1) is None
    assert controller.confirm_force(force, 7, 11, 1)
    assert controller.claim(force, 7, 11, 1) == "force"


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
