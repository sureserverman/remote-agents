from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from uuid import UUID

import pytest
from stop_results import a_clean_stop, a_stop_that_did_not_take

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.adapters.telegram.stops import StopController
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_actions import (
    GRACEFUL_TIMEOUT,
    UNKNOWN_SESSION,
    stop_failure,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


class FakeService:
    def __init__(self) -> None:
        self.actions: list[str] = []

    async def graceful_stop(self, _command):
        self.actions.append("graceful")
        return a_clean_stop()

    async def cleanup(self, _command) -> None:
        self.actions.append("cleanup")

    async def force_stop(self, _command) -> None:
        self.actions.append("force")


class Record:
    def __init__(self, session_id: SessionId, state: SessionState, profile_id: ProfileId) -> None:
        self.session_id = session_id
        self.state = state
        self.profile_id = profile_id


def test_stop_actions_require_the_right_confirmation_strength_and_claim_once() -> None:
    callbacks = CallbackStateStore()
    controller = StopController(callbacks)
    session = SessionId(UUID(int=1))
    graceful = controller.offer(
        session, ProfileId("claude"), SessionState.RUNNING, "graceful", 7, 11
    )
    force = controller.offer(session, ProfileId("claude"), SessionState.RUNNING, "force", 7, 11)
    callbacks.bind_pending(11, 1)

    claimed = controller.claim(graceful, 7, 11, 1)
    assert claimed is not None
    assert (claimed.action, claimed.session_id, claimed.profile_id) == (
        "graceful",
        session,
        ProfileId("claude"),
    )
    assert controller.claim(graceful, 7, 11, 1) is None
    # An unconfirmed force is not claimable at all: it only opens the confirmation screen,
    # whose own button is a separate token carrying a separate action.
    assert controller.claim(force, 7, 11, 1) is None
    confirmed = controller.offer_confirmed_force(
        session, ProfileId("claude"), SessionState.RUNNING, 7, 11
    )
    assert confirmed is not None
    callbacks.bind_pending(11, 1)
    claimed_force = controller.claim(confirmed, 7, 11, 1)
    assert claimed_force is not None and claimed_force.action == "force"
    assert controller.claim(confirmed, 7, 11, 1) is None


def test_cleanup_only_exists_for_preserved_sessions() -> None:
    controller = StopController(CallbackStateStore())
    session = SessionId(UUID(int=1))

    assert (
        controller.offer(session, ProfileId("claude"), SessionState.RUNNING, "cleanup", 7, 11)
        is None
    )
    assert controller.offer(session, ProfileId("claude"), SessionState.PRESERVED, "cleanup", 7, 11)


@pytest.mark.asyncio
async def test_force_stop_is_available_for_a_failed_session_that_needs_cleanup() -> None:
    callbacks = CallbackStateStore()
    controller = StopController(callbacks)
    session = SessionId(UUID(int=1))
    token = controller.offer(session, ProfileId("codex"), SessionState.FAILED, "force", 7, 11)

    assert token is not None
    confirmed = controller.offer_confirmed_force(
        session, ProfileId("codex"), SessionState.FAILED, 7, 11
    )
    assert confirmed is not None
    callbacks.bind_pending(11, 1)
    request = controller.claim(confirmed, 7, 11, 1)
    assert request is not None
    service = FakeService()

    forced = await controller.execute(
        request, service, Record(session, SessionState.FAILED, ProfileId("codex"))
    )
    assert forced.dispatched
    assert service.actions == ["force"]


@pytest.mark.asyncio
async def test_claimed_action_rechecks_current_state_before_typed_service_dispatch() -> None:
    callbacks = CallbackStateStore()
    controller = StopController(callbacks)
    session = SessionId(UUID(int=1))
    token = controller.offer(session, ProfileId("claude"), SessionState.RUNNING, "graceful", 7, 11)
    callbacks.bind_pending(11, 1)
    assert token is not None
    claimed = controller.claim(token, 7, 11, 1)
    assert claimed is not None
    service = FakeService()

    result = await controller.execute(
        claimed, service, Record(session, SessionState.PRESERVED, ProfileId("claude"))
    )
    # `.dispatched`, not the result's own truthiness: `StopResult` is a dataclass, so a
    # refusal is truthy as an object and `not result` would silently pass on every outcome.
    assert not result.dispatched
    assert service.actions == []
    mismatched = await controller.execute(
        claimed, service, Record(session, SessionState.RUNNING, ProfileId("codex"))
    )
    assert not mismatched.dispatched


def _stopped_boundary(*records: SessionRecord) -> PrivateBotBoundary:
    """A boundary whose list holds exactly `records` — what remains *after* the stop ran."""

    class _Launcher:
        async def list_sessions(self):
            return list(records)

        async def refresh_readiness(self) -> None:
            return None

    return PrivateBotBoundary(
        7,
        11,
        catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),),
        launcher=_Launcher(),
    )


def _a_session(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId(UUID(int=7)),
        ProjectId("a" * 24),
        ProfileId("claude"),
        SessionDisplayIdentity("Demo", "Claude", "regular", 1),
        state,
        datetime(2026, 8, 10, tzinfo=UTC),
    )


def _labels(rendered) -> list[str]:
    return [button.text for row in rendered.keyboard for button in row]


@pytest.mark.asyncio
async def test_a_clean_stop_lands_on_list_naming_what_ended() -> None:
    """The request that started this: a stop should leave the owner where they can act next.

    The session is gone from the list because it ended, so the rows below the notice are the
    ones that are left — which is the screen the old dead end made them navigate to.
    """
    record = _a_session()
    boundary = _stopped_boundary()  # it ended, so it is no longer listed

    rendered = await boundary._stop_outcome_reply("graceful", record)

    assert rendered.text.startswith("Stopped Demo · Claude · regular · #1\n")
    assert "The session has ended." in rendered.text
    assert "<b>Sessions</b>\nNothing is running." in rendered.text
    assert "Back" not in _labels(rendered), "the list is the destination, not a stop on the way"


@pytest.mark.asyncio
async def test_a_graceful_timeout_lands_on_list_with_its_words_unchanged() -> None:
    """BL-008's rule, carried onto the list: the failure's words are passed through.

    `graceful_timeout` and `unknown_session` leave identical evidence in the record, so the
    sentence has to come from the observation rather than be re-derived from what is listed.
    Asserted against `stop_failure`'s own output, so re-wording it in the application layer
    cannot leave this test asserting a copy that has drifted.
    """
    failure = stop_failure(a_stop_that_did_not_take(GRACEFUL_TIMEOUT))
    assert failure is not None
    record = _a_session()
    boundary = _stopped_boundary(record)  # the stop did not take, so it is still listed

    rendered = await boundary._stop_outcome_reply("graceful", record, failure)

    assert rendered.text.startswith("Demo · Claude · regular · #1 is still running\n")
    assert escape(failure.summary) in rendered.text
    assert escape(failure.remedy) in rendered.text
    assert "Sessions 1/1" in rendered.text, "it did not take, so the session is still on the list"
    assert "Back" not in _labels(rendered)


@pytest.mark.asyncio
async def test_a_stop_that_failed_for_a_departed_session_lands_on_list_with_its_own_words() -> None:
    """The BL-008 branch: the record says gone, the observation says nothing was stopped.

    Reporting "Stopped X" here would assert an ending the observation contradicts, which is
    the reading DEC-006 forbids — so this branch keeps its own wording above the rows.
    """
    failure = stop_failure(a_stop_that_did_not_take(UNKNOWN_SESSION))
    assert failure is not None
    record = _a_session()
    boundary = _stopped_boundary()  # gone from the list, while the stop reported failure

    rendered = await boundary._stop_outcome_reply("graceful", record, failure)

    assert rendered.text.startswith("Demo · Claude · regular · #1 is no longer listed\n")
    assert escape(failure.summary) in rendered.text
    assert escape(failure.remedy) in rendered.text
    assert "Stopped" not in rendered.text, "nothing observed says this session was stopped"
    assert "Back" not in _labels(rendered)


@pytest.mark.asyncio
async def test_a_stop_outcome_lands_on_list_with_the_session_name_escaped() -> None:
    """The notice is escaped once, by the list. A display name is not markup we authored."""
    record = SessionRecord(
        SessionId(UUID(int=7)),
        ProjectId("a" * 24),
        ProfileId("claude"),
        SessionDisplayIdentity("<b>Demo</b>", "Claude", "regular", 1),
        SessionState.RUNNING,
        datetime(2026, 8, 10, tzinfo=UTC),
    )

    rendered = await _stopped_boundary()._stop_outcome_reply("graceful", record)

    assert "&lt;b&gt;Demo&lt;/b&gt;" in rendered.text
    assert "<b>Demo</b>" not in rendered.text
