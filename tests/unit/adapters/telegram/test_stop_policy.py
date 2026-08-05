"""The bot offers exactly what the shared policy returns — no more, no less."""

from __future__ import annotations

from uuid import UUID

import pytest

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.stops import StopController
from remote_agents.application.session_actions import available_actions
from remote_agents.domain.models import ProfileId, SessionId, SessionState


def _controller() -> StopController:
    return StopController(CallbackStateStore())


@pytest.mark.parametrize("state", list(SessionState))
def test_offer_tokenizes_exactly_the_policy_actions(state: SessionState) -> None:
    controller = _controller()
    session = SessionId(UUID(int=1))
    allowed = set(available_actions(state))

    for action in ("graceful", "cleanup", "force"):
        token = controller.offer(session, ProfileId("claude"), state, action, 7, 11, 1)
        assert (token is not None) is (action in allowed), (
            f"{action} from {state.value}: policy says {action in allowed}"
        )


def test_an_orphaned_session_can_now_be_force_stopped() -> None:
    """The reconciliation's one behavior change: ORPHANED was stoppable from neither copy."""
    controller = _controller()
    token = controller.offer(
        SessionId(UUID(int=1)), ProfileId("claude"), SessionState.ORPHANED, "force", 7, 11, 1
    )
    assert token is not None


def test_offer_still_refuses_an_action_the_policy_does_not_name() -> None:
    controller = _controller()
    for action in ("kill", "", "GRACEFUL", "force ", "restart"):
        assert (
            controller.offer(
                SessionId(UUID(int=1)), ProfileId("claude"), SessionState.RUNNING, action, 7, 11, 1
            )
            is None
        )


def test_a_starting_session_is_tokenized_for_nothing() -> None:
    """STARTING was the state the old list builder wrongly offered force from."""
    controller = _controller()
    for action in ("graceful", "cleanup", "force"):
        assert (
            controller.offer(
                SessionId(UUID(int=1)),
                ProfileId("claude"),
                SessionState.STARTING,
                action,
                7,
                11,
                1,
            )
            is None
        )
