"""Server-side callback state that keeps Telegram payloads opaque and replay-safe."""

from __future__ import annotations

import logging
import secrets
from dataclasses import replace

from remote_agents.ports.callback_state import UNBOUND, CallbackState

_LOG = logging.getLogger(__name__)


class CallbackStateStore:
    """Hold all callback meaning locally; the Telegram token is an unguessable lookup key.

    The in-memory sibling of
    :class:`~remote_agents.adapters.sqlite.callback_state_store.SQLiteCallbackStateStore`,
    for a composition with no database behind it. Both implement `CallbackStatePort`, and
    the service composes the durable one — a button that dies on restart was the defect
    this pair exists to remove, and only the durable half actually removes it.

    Two things this deliberately no longer has, and the third that replaces them:

    - **No TTL.** A token was valid for fifteen minutes; now it is valid for as long as the
      message it was drawn on. Age is never a reason to refuse a press.
    - **No chat-global view revision.** One counter per chat meant any newer screen killed
      every button on every older message, seconds after they were drawn.
    - **Message scoping instead of both.** A token belongs to one message. A screen that is
      gone takes its tokens with it (`prune_for_message`), so a stale view is not detected
      after the fact — it stops existing.

    Replay safety is unchanged and now rests entirely on `claim_mutation`, which still
    admits exactly one caller per mutating token (DEC-007's mitigation on this
    surface: no repeated press destroys anything).
    """

    def __init__(self, *, limit: int = 20_000) -> None:
        if limit < 1:
            raise ValueError("callback state capacity must be positive")
        self._limit = limit
        self._states: dict[str, CallbackState] = {}
        self._claimed: set[str] = set()

    def create(
        self,
        action: str,
        entity_id: str,
        owner_id: int,
        chat_id: int,
        message_id: int = UNBOUND,
        *,
        mutation: bool = False,
    ) -> str:
        if not action or not entity_id or message_id < 0:
            raise ValueError("callback state must contain a safe action, entity, and message")
        self._evict_over_capacity()
        token = f"c1_{secrets.token_urlsafe(18)}"
        self._states[token] = CallbackState(
            token, action, entity_id, owner_id, chat_id, message_id, mutation
        )
        return token

    def bind_pending(self, chat_id: int, message_id: int) -> int:
        """Attach this chat's freshly minted tokens to the message that now carries them."""
        if message_id <= UNBOUND:
            raise ValueError("a bound callback message must be a real Telegram message")
        pending = [
            token
            for token, state in self._states.items()
            if state.chat_id == chat_id and state.message_id == UNBOUND
        ]
        for token in pending:
            self._states[token] = replace(self._states[token], message_id=message_id)
        return len(pending)

    def resolve(
        self, token: str, *, owner_id: int, chat_id: int, message_id: int
    ) -> CallbackState | None:
        state = self._states.get(token)
        if (
            state is None
            or owner_id != state.owner_id
            or chat_id != state.chat_id
            or message_id != state.message_id
        ):
            return None
        return state

    def claim_mutation(self, token: str, *, owner_id: int, chat_id: int, message_id: int) -> bool:
        """The process-local twin of the SQLite claim, and it must agree about the message.

        `message_id` is accepted and not matched on, for the reason the durable twin gives at
        length: `resolve` has already enforced message binding, and re-checking it here broke
        across the `rebind` that `LiveView.move_to_bottom` performs. A twin that kept checking
        would answer differently from the store the service actually runs on, which is worse
        than either behaviour on its own.
        """
        del message_id
        state = self._states.get(token)
        if (
            state is None
            or state.owner_id != owner_id
            or state.chat_id != chat_id
            or not state.mutation
            or token in self._claimed
        ):
            return False
        self._claimed.add(token)
        return True

    def prune_for_message(self, chat_id: int, message_id: int) -> int:
        """Discard the tokens of a message that no longer exists, and report how many."""
        doomed = [
            token
            for token, state in self._states.items()
            if state.chat_id == chat_id and state.message_id == message_id
        ]
        for token in doomed:
            self._discard(token)
        return len(doomed)

    def rebind(self, chat_id: int, from_message_id: int, to_message_id: int) -> int:
        """Move a message's tokens onto the message replacing it, and report how many."""
        if to_message_id <= UNBOUND:
            raise ValueError("a rebound callback message must be a real Telegram message")
        moved = [
            token
            for token, state in self._states.items()
            if state.chat_id == chat_id and state.message_id == from_message_id
        ]
        for token in moved:
            self._states[token] = replace(self._states[token], message_id=to_message_id)
        return len(moved)

    def active_count(self) -> int:
        """Expose the number of live callback states for bounded-resource verification.

        A method rather than the property this used to be, so both implementations of
        `CallbackStatePort` are callable the same way — a durable store cannot make this a
        property without hiding a query behind an attribute access.
        """

        return len(self._states)

    def _evict_over_capacity(self) -> None:
        """Keep the store bounded by size, since it is no longer bounded by time.

        Refusing to mint once full — the previous behaviour — turns a full store into a dead
        keyboard. Evicting the oldest degrades the only thing that can be degraded: a button
        nobody has pressed in twenty thousand renders.
        """
        surplus = len(self._states) - self._limit + 1
        if surplus <= 0:
            return
        for token in list(self._states)[:surplus]:
            self._discard(token)
        _LOG.info("evicted %d callback states over the %d capacity", surplus, self._limit)

    def _discard(self, token: str) -> None:
        del self._states[token]
        self._claimed.discard(token)
