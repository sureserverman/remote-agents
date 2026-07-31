"""Application use cases driven only by typed ports and domain rules."""

from __future__ import annotations

from datetime import UTC, datetime

from remote_agents.application.commands import (
    CleanupCommand,
    ForceStopCommand,
    GracefulStopCommand,
    InspectQuery,
    LaunchCommand,
)
from remote_agents.application.errors import DuplicateCommandError, SessionNotFoundError
from remote_agents.application.reconcile import SessionLocks
from remote_agents.domain.models import (
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.state_machine import LifecycleEvent, transition
from remote_agents.ports.session_store import SessionStore
from remote_agents.ports.terminal import TerminalObservation, TerminalPort


class SessionService:
    """Typed operations whose liveness authority is always TerminalPort."""

    def __init__(
        self, store: SessionStore, terminal: TerminalPort, *, locks: SessionLocks | None = None
    ) -> None:
        self._store = store
        self._terminal = terminal
        self._locks = locks or SessionLocks()

    async def launch(self, command: LaunchCommand) -> SessionRecord:
        async with self._locks.operation():
            if not await self._store.claim_idempotency_key(command.idempotency_key):
                raise DuplicateCommandError("launch callback was already handled")
            session_id = SessionId.new()
            sequence = await self._store.next_sequence(command.project_id, command.profile_id)
            record = SessionRecord(
                session_id,
                command.project_id,
                command.profile_id,
                SessionDisplayIdentity(
                    str(command.project_id),
                    str(command.profile_id),
                    "regular",
                    sequence,
                    command.label,
                ),
                SessionState.STARTING,
                datetime.now(UTC),
            )
            await self._store.save(record)
            observation = await self._terminal.launch(
                session_id, command.project_id, command.profile_id
            )
            event = LifecycleEvent.READY if observation.live else LifecycleEvent.STARTUP_ERROR
            return await self._store.record_event(session_id, event)

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return tuple(await self._store.list())

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        """Promote only failed launches whose owned panes now show readiness evidence."""
        async with self._locks.operation():
            records = tuple(await self._store.list())
            for record in records:
                if record.state is not SessionState.FAILED:
                    continue
                async with self._locks.for_session(record.session_id):
                    current = await self._require_session(record.session_id)
                    if current.state is not SessionState.FAILED:
                        continue
                    observation = await self._terminal.confirm_ready(
                        current.session_id, current.profile_id
                    )
                    if observation.live:
                        await self._store.record_event(current.session_id, LifecycleEvent.READY)
            return tuple(await self._store.list())

    async def inspect(self, query: InspectQuery) -> TerminalObservation | None:
        return await self._terminal.inspect(query.session_id)

    async def graceful_stop(self, command: GracefulStopCommand) -> TerminalObservation:
        async with self._locks.operation(), self._locks.for_session(command.session_id):
            record = await self._require_session(command.session_id)
            transition(record.state, LifecycleEvent.GRACEFUL_STOP_REQUESTED)
            await self._store.record_event(
                command.session_id, LifecycleEvent.GRACEFUL_STOP_REQUESTED
            )
            observation = await self._terminal.graceful_stop(command.session_id, command.profile_id)
            if observation.preserved:
                await self._store.record_event(command.session_id, LifecycleEvent.PANE_EXITED)
            return observation

    async def cleanup(self, command: CleanupCommand) -> None:
        async with self._locks.operation(), self._locks.for_session(command.session_id):
            record = await self._require_session(command.session_id)
            transition(record.state, LifecycleEvent.CLEANUP_CONFIRMED)
            await self._terminal.cleanup(command.session_id)
            await self._store.record_event(command.session_id, LifecycleEvent.CLEANUP_CONFIRMED)

    async def force_stop(self, command: ForceStopCommand) -> TerminalObservation:
        async with self._locks.operation(), self._locks.for_session(command.session_id):
            record = await self._require_session(command.session_id)
            transition(record.state, LifecycleEvent.VERIFIED_FORCE_STOP)
            observation = await self._terminal.force_stop(command.session_id)
            await self._store.record_event(command.session_id, LifecycleEvent.VERIFIED_FORCE_STOP)
            return observation

    async def _require_session(self, session_id: SessionId) -> SessionRecord:
        record = await self._store.get(session_id)
        if record is None:
            raise SessionNotFoundError(str(session_id))
        return record
