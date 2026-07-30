"""Replay-safe confirmation state for the approved destructive session actions."""

from __future__ import annotations

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.domain.models import ProfileId, SessionId, SessionState


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
        if action not in {"graceful", "cleanup", "force"}:
            return None
        if action == "cleanup" and state is not SessionState.PRESERVED:
            return None
        if action == "graceful" and state is not SessionState.RUNNING:
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

    def claim(self, token: str, owner_id: int, chat_id: int, view_revision: int) -> str | None:
        state = self._callbacks.resolve(
            token, owner_id=owner_id, chat_id=chat_id, view_revision=view_revision
        )
        if state is None or (state.action == "force" and token not in self._force_confirmed):
            return None
        return (
            state.action
            if self._callbacks.claim_mutation(
                token, owner_id=owner_id, chat_id=chat_id, view_revision=view_revision
            )
            else None
        )
