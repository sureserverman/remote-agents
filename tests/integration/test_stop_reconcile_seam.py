"""The seam between an owner's graceful stop and the reconciliation pass beside it.

Every reconciler test drives `ReconciliationService` alone, and every stop test drives
`SessionService` alone, so the one place they meet was untested from both sides -- which is
a pattern the owner's private notes record.

What lived in that gap reached production: `journalctl --user -u remote-agents.service`
carried five `InvalidTransition` crashes, the last on 2026-08-15, each surfacing to the owner
as "callback action failed while its pending notice was on screen":

    InvalidTransition: pane_exited is not legal while session is running

`SessionService.graceful_stop` holds `for_session` across two `record_event` calls with an
`await` on the terminal between them. `ReconciliationService` runs on a timer beside the
service and writes `record_event` directly. With no lock in common, a pass landing in that
await overwrote the state the stop was about to write from.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from remote_agents.application.commands import GracefulStopCommand
from remote_agents.application.reconcile import ReconciliationService, SessionLocks
from remote_agents.application.services import SessionService
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

pytestmark = pytest.mark.asyncio


class _Store:
    def __init__(self, record: SessionRecord) -> None:
        self.records = {record.session_id: record}
        self.events: list[LifecycleEvent] = []

    async def get(self, session_id: SessionId) -> SessionRecord | None:
        return self.records.get(session_id)

    async def list(self) -> tuple[SessionRecord, ...]:
        return tuple(self.records.values())

    async def record_event(self, session_id: SessionId, event: LifecycleEvent) -> SessionRecord:
        current = self.records[session_id]
        # The real store re-reads the state and validates the transition against it, which is
        # what raised in production. A fake that skipped that could not reproduce the crash.
        updated = SessionRecord(
            current.session_id,
            current.project_id,
            current.profile_id,
            current.display,
            transition(current.state, event).to_state,
            current.created_at,
        )
        self.records[session_id] = updated
        self.events.append(event)
        return updated


class _BlockingTerminal:
    """A terminal whose graceful stop parks mid-operation, exactly where the await is."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def graceful_stop(self, session_id: SessionId, profile_id: ProfileId):
        self.entered.set()
        await self.release.wait()
        return TerminalObservation(session_id, live=False, preserved=True)

    async def cleanup(self, session_id: SessionId) -> None:
        return None


def _running_record() -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.RUNNING,
        # Older than any settle window, which is the condition under which the old guard
        # switched itself off. A fresh record would have been protected by accident.
        datetime.now(UTC) - timedelta(hours=6),
    )


async def test_a_reconcile_pass_landing_inside_a_graceful_stop_does_not_crash_it() -> None:
    record = _running_record()
    store = _Store(record)
    locks = SessionLocks()
    terminal = _BlockingTerminal()
    service = SessionService(store, terminal, locks=locks)
    reconciler = ReconciliationService(store, settle_after=timedelta(0), locks=locks)

    stop = asyncio.create_task(
        service.graceful_stop(GracefulStopCommand(record.session_id, record.profile_id))
    )
    await terminal.entered.wait()

    # The stop is now parked between its two writes, holding the session lock, and the pane
    # reads as gone. This is the pass that used to overwrite it.
    gone = TerminalObservation(record.session_id, live=False, preserved=False)
    await reconciler.reconcile((gone,))

    terminal.release.set()
    await stop

    assert store.records[record.session_id].state is SessionState.ENDED
    assert LifecycleEvent.PANE_EXITED in store.events
