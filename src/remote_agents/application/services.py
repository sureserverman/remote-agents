"""Application use cases driven only by typed ports and domain rules."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from remote_agents.application.commands import (
    CleanupCommand,
    ForceStopCommand,
    GracefulStopCommand,
    InspectQuery,
    LaunchCommand,
    RemoteControlCommand,
    ResumeCommand,
)
from remote_agents.application.errors import DuplicateCommandError, SessionNotFoundError
from remote_agents.application.reconcile import SessionLocks
from remote_agents.domain.models import (
    ProfileId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.domain.state_machine import LifecycleEvent, transition
from remote_agents.ports.session_store import ProjectUsage, SessionStore
from remote_agents.ports.terminal import TerminalObservation, TerminalPort


class SessionService:
    """Typed operations whose liveness authority is always TerminalPort."""

    def __init__(
        self,
        store: SessionStore,
        terminal: TerminalPort,
        *,
        locks: SessionLocks | None = None,
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

    async def resume(self, command: ResumeCommand) -> SessionRecord:
        """Create one managed identity for a server-resolved provider conversation."""
        async with self._locks.operation():
            async with self._locks.for_conversation(
                command.profile_id, command.conversation.provider_conversation_id
            ):
                return await self._resume_locked(command)

    async def _resume_locked(self, command: ResumeCommand) -> SessionRecord:
        """Create or return a durable resume identity while its conversation lock is held."""
        source_id = command.conversation.provider_conversation_id.value
        existing = await self._store.get_by_resume_source(command.profile_id, source_id)
        if existing is not None:
            return existing
        if not await self._store.claim_idempotency_key(command.idempotency_key):
            raise DuplicateCommandError("resume callback was already handled")
        session_id = SessionId.new()
        sequence = await self._store.next_sequence(command.project_id, command.profile_id)
        record = SessionRecord(
            session_id,
            command.project_id,
            command.profile_id,
            SessionDisplayIdentity(
                str(command.project_id), str(command.profile_id), "resumed", sequence
            ),
            SessionState.STARTING,
            datetime.now(UTC),
            command.conversation.summary.profile_id,
            source_id,
        )
        await self._store.save(record)
        observation = await self._terminal.resume(
            session_id,
            command.project_id,
            command.profile_id,
            command.conversation.provider_conversation_id,
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

    async def rename(self, session_id: SessionId, label: str | None) -> SessionRecord:
        """Name a running session, or clear its name. Metadata only — nothing is signalled.

        Under the session lock like every other mutation of a record, so a rename cannot
        interleave with a stop walking the same row to ENDED. It deliberately does not check
        the state: naming an ended session is harmless and the list still shows it until
        reconciliation removes it, so refusing would be a rule with nothing behind it.
        """
        async with self._locks.for_session(session_id):
            await self._require_session(session_id)
            return await self._store.set_label(session_id, label)

    async def project_usage(self) -> Sequence[ProjectUsage]:
        """Per-project launch history, for ordering the pickers by what is actually used.

        A pass-through to the store rather than a computation: the ranking is a pure function
        the caller applies, and putting the decay here would tie the order to whoever asked
        instead of to the screen being drawn.
        """
        return await self._store.project_usage()

    async def inspect(self, query: InspectQuery) -> TerminalObservation | None:
        return await self._terminal.inspect(query.session_id)

    async def copy_attach(self, session_id: SessionId) -> str | None:
        """Return a copyable command only after current record and terminal ownership agree."""
        record = await self._require_session(session_id)
        observation = await self._terminal.inspect(session_id)
        if (
            observation is None
            or not observation.live
            or observation.project_id != record.project_id
            or observation.profile_id != record.profile_id
        ):
            return None
        return await self._terminal.copy_attach(session_id)

    async def set_remote_control(self, command: RemoteControlCommand) -> RemoteControlState:
        """Execute a profile-owned state transition only once for an exact Claude session."""
        async with self._locks.operation(), self._locks.for_session(command.session_id):
            record = await self._require_session(command.session_id)
            if record.profile_id != ProfileId("claude"):
                raise ValueError("remote control is available only for Claude")
            if not await self._store.claim_idempotency_key(command.idempotency_key):
                raise DuplicateCommandError("remote control callback was already handled")
            return await self._terminal.remote_control(command.session_id, command.desired_state)

    async def graceful_stop(self, command: GracefulStopCommand) -> TerminalObservation:
        """Stop the agent on its own terms and remove its pane in the same operation.

        A stop the owner asked for ends the session: PRESERVED is passed through so the
        recorded history still says the pane exited before it was removed, but the owner
        is not asked to close it in a second confirmed step. The cost is deliberate — the
        pane's output stops being capturable here, so nothing can be inspected after a
        graceful stop. A pane that dies on its own still lands in PRESERVED and stays
        there (RECONCILED_PANE_DEAD), which is the path that keeps output to read.

        The returned observation describes the *stop*, not what survives it: `preserved`
        means the profile's own exit sequence worked, and remains the way a caller tells
        a clean exit from `graceful_timeout`. On timeout nothing is removed and the
        session returns to RUNNING, so force stop stays a separately chosen action.

        A cleanup that fails raises with the session left in PRESERVED, where Clean up is
        still offered — a stop reported as complete over a pane still holding a terminal
        would be the worse answer.
        """
        async with self._locks.operation(), self._locks.for_session(command.session_id):
            record = await self._require_session(command.session_id)
            transition(record.state, LifecycleEvent.GRACEFUL_STOP_REQUESTED)
            await self._store.record_event(
                command.session_id, LifecycleEvent.GRACEFUL_STOP_REQUESTED
            )
            observation = await self._terminal.graceful_stop(command.session_id, command.profile_id)
            if not observation.preserved:
                await self._store.record_event(
                    command.session_id, LifecycleEvent.GRACEFUL_STOP_TIMED_OUT
                )
                return observation
            await self._store.record_event(command.session_id, LifecycleEvent.PANE_EXITED)
            await self._terminal.cleanup(command.session_id)
            await self._store.record_event(command.session_id, LifecycleEvent.CLEANUP_CONFIRMED)
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
