"""Replay-safe confirmation state for the approved destructive session actions."""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.application.commands import CleanupCommand, ForceStopCommand, GracefulStopCommand
from remote_agents.application.session_actions import available_actions
from remote_agents.domain.models import ProfileId, SessionId, SessionState


@dataclass(frozen=True, slots=True)
class StopRequest:
    action: str
    session_id: SessionId
    profile_id: ProfileId


class StopController:
    def __init__(self, callbacks: CallbackStateStore) -> None:
        self._callbacks = callbacks
        self._force_confirmed: set[str] = set()

    def offer(
        self,
        session_id: SessionId,
        profile_id: ProfileId,
        state: SessionState,
        action: str,
        owner_id: int,
        chat_id: int,
        view_revision: int,
    ) -> str | None:
        if action not in available_actions(state):
            return None
        return self._callbacks.create(
            action, f"{session_id}:{profile_id}", owner_id, chat_id, view_revision, mutation=True
        )

    def confirm_force(self, token: str, owner_id: int, chat_id: int, view_revision: int) -> bool:
        state = self._callbacks.resolve(
            token, owner_id=owner_id, chat_id=chat_id, view_revision=view_revision
        )
        if state is None or state.action != "force":
            return False
        self._force_confirmed.add(token)
        return True

    def claim(
        self, token: str, owner_id: int, chat_id: int, view_revision: int
    ) -> StopRequest | None:
        state = self._callbacks.resolve(
            token, owner_id=owner_id, chat_id=chat_id, view_revision=view_revision
        )
        if state is None or (state.action == "force" and token not in self._force_confirmed):
            return None
        if not self._callbacks.claim_mutation(
            token, owner_id=owner_id, chat_id=chat_id, view_revision=view_revision
        ):
            return None
        session_value, profile_value = state.entity_id.split(":", maxsplit=1)
        return StopRequest(state.action, SessionId.parse(session_value), ProfileId(profile_value))

    async def execute(self, request: StopRequest, service: object, record: object) -> bool:
        """Recheck the current record before dispatching one typed destructive command."""

        if record.session_id != request.session_id or record.profile_id != request.profile_id:
            return False
        # The record is re-read here because it may have moved on since the token was
        # issued, but the rule it is checked against is the shared one. A private copy is
        # what let an offered action be silently refused at dispatch.
        if request.action not in available_actions(record.state):
            return False
        if request.action == "graceful":
            await service.graceful_stop(GracefulStopCommand(request.session_id, request.profile_id))
            return True
        if request.action == "cleanup":
            await service.cleanup(CleanupCommand(request.session_id))
            return True
        await service.force_stop(ForceStopCommand(request.session_id))
        return True
