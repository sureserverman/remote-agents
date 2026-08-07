"""A session recorded as failed while its pane works must be able to recover."""

from datetime import UTC, datetime

import pytest

from remote_agents.application.reconcile import _event_for_reconciliation, reconcile
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.state_machine import LifecycleEvent, transition
from remote_agents.ports.terminal import TerminalObservation


def _record(session_id: SessionId, state: SessionState) -> SessionRecord:
    return SessionRecord(
        session_id,
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "resumed", 1),
        state,
        datetime.now(UTC),
    )


def _live(session_id: SessionId) -> TerminalObservation:
    return TerminalObservation(session_id, live=True, preserved=False)


def test_a_failed_session_whose_pane_is_live_is_promoted_back_to_running() -> None:
    """The case a resumed session lands in: recorded failed, demonstrably working."""
    session_id = SessionId.new()
    record = _record(session_id, SessionState.FAILED)

    (result,) = reconcile((record,), (_live(session_id),))
    event = _event_for_reconciliation(record, result)

    assert event is LifecycleEvent.READY
    assert transition(record.state, event).to_state is SessionState.RUNNING


def test_a_failed_session_whose_pane_is_gone_stays_failed() -> None:
    """Promotion must rest on the pane, not on optimism about it."""
    session_id = SessionId.new()
    record = _record(session_id, SessionState.FAILED)

    (result,) = reconcile((record,), ())
    event = _event_for_reconciliation(record, result)

    assert event is None
    assert result.state is SessionState.ENDED


@pytest.mark.parametrize(
    "state", [SessionState.STARTING, SessionState.FAILED, SessionState.RUNNING]
)
def test_promotion_never_produces_an_illegal_transition(state: SessionState) -> None:
    session_id = SessionId.new()
    record = _record(session_id, state)

    (result,) = reconcile((record,), (_live(session_id),))
    event = _event_for_reconciliation(record, result)

    if event is not None:
        assert transition(record.state, event).to_state is SessionState.RUNNING
