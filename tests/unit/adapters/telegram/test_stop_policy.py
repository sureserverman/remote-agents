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


def test_an_orphaned_session_is_offered_no_stop_at_all() -> None:
    """ORPHANED is quarantined for local attention; the domain permits no event from it.

    Tokenizing a force here would hand the owner a button that raises InvalidTransition
    instead of stopping anything.
    """
    controller = _controller()
    for action in ("graceful", "cleanup", "force"):
        assert (
            controller.offer(
                SessionId(UUID(int=1)),
                ProfileId("claude"),
                SessionState.ORPHANED,
                action,
                7,
                11,
                1,
            )
            is None
        )


def test_offer_still_refuses_an_action_the_policy_does_not_name() -> None:
    controller = _controller()
    for action in ("kill", "", "GRACEFUL", "force ", "restart"):
        assert (
            controller.offer(
                SessionId(UUID(int=1)), ProfileId("claude"), SessionState.RUNNING, action, 7, 11, 1
            )
            is None
        )


class _RecordingService:
    def __init__(self) -> None:
        self.dispatched: list[str] = []

    async def graceful_stop(self, _command) -> None:
        self.dispatched.append("graceful")

    async def cleanup(self, _command) -> None:
        self.dispatched.append("cleanup")

    async def force_stop(self, _command) -> None:
        self.dispatched.append("force")


class _Record:
    def __init__(self, session_id: SessionId, profile_id: ProfileId, state: SessionState) -> None:
        self.session_id = session_id
        self.profile_id = profile_id
        self.state = state


@pytest.mark.parametrize("state", list(SessionState))
@pytest.mark.parametrize("action", ["graceful", "cleanup", "force"])
async def test_execute_dispatches_exactly_what_the_policy_permits(
    state: SessionState, action: str
) -> None:
    """`execute` rechecks the live record — it must recheck against the shared policy.

    Offering an action the executor then refuses is the failure the ORPHANED force button
    had: the token was issued, the owner confirmed, and the stop silently no-opped.
    """
    from remote_agents.adapters.telegram.stops import StopRequest

    session = SessionId(UUID(int=1))
    profile = ProfileId("claude")
    service = _RecordingService()
    request = StopRequest(action, session, profile)

    dispatched = await _controller().execute(request, service, _Record(session, profile, state))

    assert dispatched is (action in available_actions(state))
    assert service.dispatched == ([action] if action in available_actions(state) else [])


async def test_an_orphaned_force_never_reaches_the_service() -> None:
    """A forged or stale token must not drive a stop the domain would reject.

    `execute` is the last gate before the terminal, so it is where a token that should
    never have been issued has to die.
    """
    from remote_agents.adapters.telegram.stops import StopRequest

    session = SessionId(UUID(int=1))
    profile = ProfileId("claude")
    service = _RecordingService()

    dispatched = await _controller().execute(
        StopRequest("force", session, profile),
        service,
        _Record(session, profile, SessionState.ORPHANED),
    )

    assert dispatched is False
    assert service.dispatched == []


async def test_execute_still_refuses_a_record_that_is_not_the_requested_session() -> None:
    """The identity recheck is defence in depth and must survive the policy rewire."""
    from remote_agents.adapters.telegram.stops import StopRequest

    service = _RecordingService()
    dispatched = await _controller().execute(
        StopRequest("force", SessionId(UUID(int=1)), ProfileId("claude")),
        service,
        _Record(SessionId(UUID(int=2)), ProfileId("claude"), SessionState.RUNNING),
    )
    assert dispatched is False
    assert service.dispatched == []


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
