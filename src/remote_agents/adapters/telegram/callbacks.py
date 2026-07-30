"""Server-side callback state that keeps Telegram payloads opaque and replay-safe."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True, slots=True)
class CallbackState:
    action: str
    entity_id: str
    owner_id: int
    chat_id: int
    view_revision: int
    expires_at: datetime
    mutation: bool


class CallbackStateStore:
    """Hold all callback meaning locally; the Telegram token is an unguessable lookup key."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        if ttl <= timedelta():
            raise ValueError("callback state TTL must be positive")
        self._now = now
        self._ttl = ttl
        self._states: dict[str, CallbackState] = {}
        self._claimed: set[str] = set()

    def create(
        self,
        action: str,
        entity_id: str,
        owner_id: int,
        chat_id: int,
        view_revision: int,
        *,
        mutation: bool = False,
    ) -> str:
        if not action or not entity_id or view_revision < 0:
            raise ValueError("callback state must contain a safe action, entity, and revision")
        token = f"c1_{secrets.token_urlsafe(18)}"
        self._states[token] = CallbackState(
            action,
            entity_id,
            owner_id,
            chat_id,
            view_revision,
            self._now() + self._ttl,
            mutation,
        )
        return token

    def resolve(
        self, token: str, *, owner_id: int, chat_id: int, view_revision: int
    ) -> CallbackState | None:
        state = self._states.get(token)
        if (
            state is None
            or self._now() >= state.expires_at
            or owner_id != state.owner_id
            or chat_id != state.chat_id
            or view_revision != state.view_revision
        ):
            return None
        return state

    def claim_mutation(
        self, token: str, *, owner_id: int, chat_id: int, view_revision: int
    ) -> bool:
        state = self.resolve(token, owner_id=owner_id, chat_id=chat_id, view_revision=view_revision)
        if state is None or not state.mutation or token in self._claimed:
            return False
        self._claimed.add(token)
        return True
