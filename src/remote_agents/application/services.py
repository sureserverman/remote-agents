"""Application use cases driven only by typed ports and domain rules."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

from remote_agents.application.commands import (
    AdoptionCommand,
    CleanupCommand,
    ExternalStopCommand,
    ForceStopCommand,
    GracefulStopCommand,
    InspectQuery,
    LaunchCommand,
    RemoteControlCommand,
    ResumeCommand,
)
from remote_agents.application.errors import (
    DuplicateCommandError,
    ExternalSessionStillRunningError,
    ExternalSessionUnavailableError,
    SessionNotFoundError,
)
from remote_agents.application.reconcile import SessionLocks
from remote_agents.domain.conversations import (
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.external_sessions import (
    ExternalSessionReference,
    ExternalSessionSummary,
    ExternalStopOutcome,
)
from remote_agents.domain.handoff_intents import HandoffIntent, HandoffState
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.domain.state_machine import LifecycleEvent, transition
from remote_agents.ports.external_process_control import ExternalProcessController
from remote_agents.ports.local_processes import LocalProcessCatalog
from remote_agents.ports.session_store import SessionStore
from remote_agents.ports.terminal import TerminalObservation, TerminalPort


class SessionService:
    """Typed operations whose liveness authority is always TerminalPort."""

    def __init__(
        self,
        store: SessionStore,
        terminal: TerminalPort,
        *,
        processes: LocalProcessCatalog | None = None,
        process_controller: ExternalProcessController | None = None,
        locks: SessionLocks | None = None,
    ) -> None:
        self._store = store
        self._terminal = terminal
        self._processes = processes
        self._process_controller = process_controller
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

    async def adopt(self, command: AdoptionCommand) -> SessionRecord:
        """Resume only after fresh evidence proves the original process exited locally."""
        if self._processes is None:
            raise ExternalSessionUnavailableError("external discovery is unavailable")
        source = command.external.provider_conversation_id
        assert source is not None
        profile_id = command.external.summary.profile_id
        project_id = command.external.summary.project_id
        assert project_id is not None
        reference = command.external.summary.reference
        async with self._locks.operation(), self._locks.for_conversation(profile_id, source):
            if await self._processes.is_still_running(reference):
                raise ExternalSessionStillRunningError(
                    "exit the external session locally before resuming"
                )
            return await self._resume_locked(
                ResumeCommand(
                    project_id,
                    profile_id,
                    _external_conversation(command.external),
                    command.idempotency_key,
                )
            )

    async def terminate_and_resume(self, command: ExternalStopCommand) -> SessionRecord:
        if self._processes is None or self._process_controller is None:
            raise ExternalSessionUnavailableError("external control is unavailable")
        external = command.external
        identity = external.identity
        assert identity is not None
        async with self._locks.operation(), self._locks.for_external(external.summary.reference):
            async with self._locks.for_conversation(
                command.conversation.summary.profile_id,
                command.conversation.provider_conversation_id,
            ):
                if not await self._store.claim_idempotency_key(command.idempotency_key):
                    raise DuplicateCommandError("external handoff callback was already handled")
                intent = HandoffIntent(
                    f"h-{command.idempotency_key}",
                    command.conversation.summary.profile_id,
                    command.conversation.summary.project_id,
                    command.conversation.provider_conversation_id.value,
                    identity,
                    HandoffState.REQUESTED,
                )
                await self._store.save_handoff_intent(intent)
                result = await self._process_controller.terminate(identity)
                if result.outcome is not ExternalStopOutcome.EXITED:
                    raise ExternalSessionUnavailableError("external process did not exit")
                await self._store.update_handoff_state(intent.intent_id, HandoffState.STOP_SENT)
                resumed = await self._resume_locked(
                    ResumeCommand(
                        command.conversation.summary.project_id,
                        command.conversation.summary.profile_id,
                        command.conversation,
                        f"resume-{command.idempotency_key}",
                    )
                )
                await self._store.update_handoff_state(intent.intent_id, HandoffState.RESUMED)
                return resumed

    async def terminate_and_resume_verified(
        self, external, idempotency_key: str
    ) -> SessionRecord:
        """Use only a provider source already correlated by the read-only adapter."""
        if external.provider_conversation_id is None or external.summary.project_id is None:
            raise ExternalSessionUnavailableError("external session lacks a verified resume source")
        return await self.terminate_and_resume(
            ExternalStopCommand(external, _external_conversation(external), idempotency_key)
        )

    async def recover_external_handoffs(self) -> tuple[SessionRecord, ...]:
        """Recover only post-signal intents; REQUESTED is never signalled after a restart."""
        if self._process_controller is None:
            return ()
        recovered: list[SessionRecord] = []
        intents = await self._store.list_handoff_intents(
            (HandoffState.REQUESTED, HandoffState.STOP_SENT)
        )
        for intent in intents:
            if intent.state is HandoffState.REQUESTED:
                await self._store.update_handoff_state(intent.intent_id, HandoffState.FAILED)
                continue
            if not await self._process_controller.is_gone(intent.process):
                continue
            conversation = _stored_conversation(
                intent.profile_id, intent.project_id, intent.conversation_source_id
            )
            async with self._locks.operation(), self._locks.for_conversation(
                intent.profile_id, conversation.provider_conversation_id
            ):
                record = await self._resume_locked(
                    ResumeCommand(
                        intent.project_id,
                        intent.profile_id,
                        conversation,
                        f"recover-{intent.intent_id}",
                    )
                )
                await self._store.update_handoff_state(intent.intent_id, HandoffState.RESUMED)
                recovered.append(record)
        return tuple(recovered)

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

    async def list_external_sessions(self) -> tuple[ExternalSessionSummary, ...]:
        if self._processes is None:
            return ()
        return await self._processes.list_external_sessions(
            excluded_process_roots=await self._terminal.managed_process_roots()
        )

    async def resolve_external_session(self, reference: ExternalSessionReference):
        if self._processes is None:
            return None
        return await self._processes.resolve_external_session(reference)

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
        async with self._locks.operation(), self._locks.for_session(command.session_id):
            record = await self._require_session(command.session_id)
            transition(record.state, LifecycleEvent.GRACEFUL_STOP_REQUESTED)
            await self._store.record_event(
                command.session_id, LifecycleEvent.GRACEFUL_STOP_REQUESTED
            )
            observation = await self._terminal.graceful_stop(command.session_id, command.profile_id)
            if observation.preserved:
                await self._store.record_event(command.session_id, LifecycleEvent.PANE_EXITED)
            else:
                await self._store.record_event(
                    command.session_id, LifecycleEvent.GRACEFUL_STOP_TIMED_OUT
                )
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


def _external_conversation(external) -> ResolvedConversation:
    """Translate verified local evidence into a typed internal continuation selection."""
    project_id = external.summary.project_id
    source = external.provider_conversation_id
    if project_id is None or source is None:
        raise ExternalSessionUnavailableError("external session is not eligible for safe handoff")
    digest = sha256(
        f"{external.summary.profile_id}\0{project_id}\0{source.value}".encode()
    ).hexdigest()
    return ResolvedConversation(
        ConversationSummary(
            ConversationReference(f"c-{digest}"),
            external.summary.profile_id,
            project_id,
            ConversationState.RESUMABLE,
            datetime.now(UTC),
        ),
        source,
    )


def _stored_conversation(
    profile_id: ProfileId, project_id: ProjectId, source_id: str
) -> ResolvedConversation:
    source = ProviderConversationId(source_id)
    digest = sha256(f"{profile_id}\0{project_id}\0{source.value}".encode()).hexdigest()
    return ResolvedConversation(
        ConversationSummary(
            ConversationReference(f"c-{digest}"),
            profile_id,
            project_id,
            ConversationState.RESUMABLE,
            datetime.now(UTC),
        ),
        source,
    )
