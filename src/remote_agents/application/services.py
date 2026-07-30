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
from remote_agents.domain.models import (
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.state_machine import LifecycleEvent
from remote_agents.ports.session_store import SessionStore
from remote_agents.ports.terminal import TerminalObservation, TerminalPort


class SessionService:
    """Typed operations whose liveness authority is always TerminalPort."""

    def __init__(self, store: SessionStore, terminal: TerminalPort) -> None:
        self._store = store
        self._terminal = terminal

    async def launch(self, command: LaunchCommand) -> SessionRecord:
        if not await self._store.claim_idempotency_key(command.idempotency_key):
            raise DuplicateCommandError("launch callback was already handled")
        session_id = SessionId.new()
        sequence = await self._store.next_sequence(command.project_id, command.profile_id)
        record = SessionRecord(
            session_id,
            command.project_id,
            command.profile_id,
            SessionDisplayIdentity(
                str(command.project_id), str(command.profile_id), "regular", sequence
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

    async def inspect(self, query: InspectQuery) -> TerminalObservation | None:
        return await self._terminal.inspect(query.session_id)

    async def graceful_stop(self, command: GracefulStopCommand) -> TerminalObservation:
        await self._require_session(command.session_id)
        await self._store.record_event(command.session_id, LifecycleEvent.GRACEFUL_STOP_REQUESTED)
        observation = await self._terminal.graceful_stop(command.session_id, command.profile_id)
        if observation.preserved:
            await self._store.record_event(command.session_id, LifecycleEvent.PANE_EXITED)
        return observation

    async def cleanup(self, command: CleanupCommand) -> None:
        await self._require_session(command.session_id)
        await self._terminal.cleanup(command.session_id)
        await self._store.record_event(command.session_id, LifecycleEvent.CLEANUP_CONFIRMED)

    async def force_stop(self, command: ForceStopCommand) -> TerminalObservation:
        await self._require_session(command.session_id)
        observation = await self._terminal.force_stop(command.session_id)
        await self._store.record_event(command.session_id, LifecycleEvent.VERIFIED_FORCE_STOP)
        return observation

    async def _require_session(self, session_id: SessionId) -> SessionRecord:
        record = await self._store.get(session_id)
        if record is None:
            raise SessionNotFoundError(str(session_id))
        return record
