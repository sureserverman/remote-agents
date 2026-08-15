"""The bot offers exactly what the shared policy returns — no more, no less."""

from __future__ import annotations

from uuid import UUID

import pytest
from stop_results import a_clean_stop, a_stop_that_did_not_take, a_verified_force_stop

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.stops import StopController
from remote_agents.application.session_actions import (
    GRACEFUL_TIMEOUT,
    UNKNOWN_SESSION,
    available_actions,
)
from remote_agents.domain.models import ProfileId, SessionId, SessionState
from remote_agents.ports.terminal import TerminalObservation


def _controller() -> StopController:
    return StopController(CallbackStateStore())


@pytest.mark.parametrize("state", list(SessionState))
def test_offer_tokenizes_exactly_the_policy_actions(state: SessionState) -> None:
    controller = _controller()
    session = SessionId(UUID(int=1))
    allowed = set(available_actions(state))

    for action in ("graceful", "cleanup", "force"):
        token = controller.offer(session, ProfileId("claude"), state, action, 7, 11)
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
            )
            is None
        )


def test_offer_still_refuses_an_action_the_policy_does_not_name() -> None:
    controller = _controller()
    for action in ("kill", "", "GRACEFUL", "force ", "restart"):
        assert (
            controller.offer(
                SessionId(UUID(int=1)), ProfileId("claude"), SessionState.RUNNING, action, 7, 11
            )
            is None
        )


class _RecordingService:
    def __init__(self) -> None:
        self.dispatched: list[str] = []

    async def graceful_stop(self, _command) -> TerminalObservation:
        self.dispatched.append("graceful")
        return a_clean_stop()

    async def cleanup(self, _command) -> None:
        self.dispatched.append("cleanup")

    async def force_stop(self, _command):
        self.dispatched.append("force")
        return a_verified_force_stop()


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

    result = await _controller().execute(request, service, _Record(session, profile, state))

    assert result.dispatched is (action in available_actions(state))
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

    result = await _controller().execute(
        StopRequest("force", session, profile),
        service,
        _Record(session, profile, SessionState.ORPHANED),
    )

    assert result.dispatched is False
    assert service.dispatched == []


async def test_execute_still_refuses_a_record_that_is_not_the_requested_session() -> None:
    """The identity recheck is defence in depth and must survive the policy rewire."""
    from remote_agents.adapters.telegram.stops import StopRequest

    service = _RecordingService()
    result = await _controller().execute(
        StopRequest("force", SessionId(UUID(int=1)), ProfileId("claude")),
        service,
        _Record(SessionId(UUID(int=2)), ProfileId("claude"), SessionState.RUNNING),
    )
    assert result.dispatched is False
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
            )
            is None
        )


# BL-008 on this surface — `execute` reports *why* a graceful stop did not take effect -------
#
# `StopController.execute` answered a bare bool, so the one thing `graceful_stop`'s
# observation carries — whether the profile's own exit sequence actually worked, and if not
# which of two unrelated causes it was — was discarded here exactly as it was on the local
# surface. The bot then inferred "it did not exit in time" from the session still being
# listed, which is right for one cause and confidently wrong for the other.


class _FailingService:
    """A graceful stop that reports back the cause the test named."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        self.dispatched: list[str] = []

    async def graceful_stop(self, _command) -> TerminalObservation:
        self.dispatched.append("graceful")
        return a_stop_that_did_not_take(self.detail)

    async def cleanup(self, _command) -> None:  # pragma: no cover - not reached
        self.dispatched.append("cleanup")

    async def force_stop(self, _command):  # pragma: no cover - not reached
        self.dispatched.append("force")
        return a_verified_force_stop()


@pytest.mark.parametrize("detail", [UNKNOWN_SESSION, GRACEFUL_TIMEOUT])
async def test_execute_reports_why_a_graceful_stop_did_not_take_effect(detail: str) -> None:
    from remote_agents.adapters.telegram.stops import StopRequest

    session = SessionId(UUID(int=1))
    profile = ProfileId("claude")
    service = _FailingService(detail)

    result = await _controller().execute(
        StopRequest("graceful", session, profile),
        service,
        _Record(session, profile, SessionState.RUNNING),
    )

    assert result.dispatched is True, "the command ran; only its outcome was a failure"
    assert result.failure is not None
    assert result.failure.detail == detail


async def test_execute_reports_no_failure_when_the_stop_worked() -> None:
    """The other direction: a clean exit must not be reported as suspect.

    `_RecordingService` answers a preserved observation, which is what the real service
    returns when the profile's own exit sequence ran.
    """
    from remote_agents.adapters.telegram.stops import StopRequest

    session = SessionId(UUID(int=1))
    profile = ProfileId("claude")

    result = await _controller().execute(
        StopRequest("graceful", session, profile),
        _RecordingService(),
        _Record(session, profile, SessionState.RUNNING),
    )

    assert result.dispatched is True
    assert result.failure is None


@pytest.mark.parametrize("action", ["cleanup", "force"])
async def test_only_a_graceful_stop_can_report_a_failure(action: str) -> None:
    """`cleanup` returns nothing and `force` kills; neither has two causes to tell apart.

    Pinned so a later change that starts reading their return values has to say so here
    rather than quietly reporting a force as a stop that did not take effect.
    """
    from remote_agents.adapters.telegram.stops import StopRequest

    session = SessionId(UUID(int=1))
    profile = ProfileId("claude")
    state = SessionState.PRESERVED if action == "cleanup" else SessionState.RUNNING

    result = await _controller().execute(
        StopRequest(action, session, profile),
        _RecordingService(),
        _Record(session, profile, state),
    )

    assert result.dispatched is True
    assert result.failure is None
