"""Safe reconciliation policy for durable records and trusted terminal observations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from remote_agents.domain.models import SessionId, SessionRecord, SessionState
from remote_agents.ports.terminal import TerminalObservation


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    session_id: SessionId
    state: SessionState
    reason: str


def reconcile(
    records: tuple[SessionRecord, ...], observations: tuple[TerminalObservation, ...]
) -> tuple[ReconciliationResult, ...]:
    """Derive safe states from terminal evidence without terminal mutation."""
    by_id = {observation.session_id: observation for observation in observations}
    results: list[ReconciliationResult] = []
    for record in records:
        observation = by_id.get(record.session_id)
        if observation is None:
            state, reason = SessionState.ENDED, "terminal_missing"
        elif observation.preserved:
            state, reason = SessionState.PRESERVED, "pane_dead"
        elif observation.live:
            state, reason = SessionState.RUNNING, "terminal_live"
        else:
            state, reason = SessionState.ORPHANED, "ambiguous_terminal"
        results.append(ReconciliationResult(record.session_id, state, reason))
    known = {record.session_id for record in records}
    results.extend(
        ReconciliationResult(observation.session_id, SessionState.ORPHANED, "unknown_session")
        for observation in observations
        if observation.session_id not in known
    )
    return tuple(results)


class SessionLocks:
    """Per-session asyncio locks serializing concurrent destructive mutations."""

    def __init__(self) -> None:
        self._locks: dict[SessionId, asyncio.Lock] = {}

    def for_session(self, session_id: SessionId) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())
