"""Reconciliation tests: terminal evidence wins and ambiguity is read-only."""

import asyncio
from datetime import UTC, datetime

from remote_agents.application.reconcile import ReconciliationResult, SessionLocks, reconcile
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.terminal import TerminalObservation


def record(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


def test_reconcile_treats_terminal_as_liveness_authority() -> None:
    live, missing, preserved, ambiguous = record(), record(), record(), record()
    observations = (
        TerminalObservation(live.session_id, live=True, preserved=False),
        TerminalObservation(preserved.session_id, live=False, preserved=True),
        TerminalObservation(ambiguous.session_id, live=False, preserved=False),
    )

    results = {
        result.session_id: result
        for result in reconcile((live, missing, preserved, ambiguous), observations)
    }

    assert results[live.session_id].state is SessionState.RUNNING
    assert results[missing.session_id].state is SessionState.ENDED
    assert results[preserved.session_id].state is SessionState.PRESERVED
    assert results[ambiguous.session_id].state is SessionState.ORPHANED


def test_reconcile_quarantines_unknown_terminal_session() -> None:
    unknown = SessionId.new()

    result = reconcile((), (TerminalObservation(unknown, live=True, preserved=False),))

    assert result == (ReconciliationResult(unknown, SessionState.ORPHANED, "unknown_session"),)


async def test_per_session_lock_serializes_concurrent_mutations() -> None:
    session_id = SessionId.new()
    locks = SessionLocks()
    order: list[str] = []

    async def mutate(name: str) -> None:
        async with locks.for_session(session_id):
            order.append(f"{name}-start")
            await asyncio.sleep(0)
            order.append(f"{name}-end")

    await asyncio.gather(mutate("first"), mutate("second"))

    assert order in (
        ["first-start", "first-end", "second-start", "second-end"],
        ["second-start", "second-end", "first-start", "first-end"],
    )
