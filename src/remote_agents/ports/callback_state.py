"""Technology-neutral durable contract for opaque control-surface callback tokens."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CallbackState:
    """What one opaque token means, resolved server-side.

    There is no expiry field. A token is valid for as long as the message it was drawn on,
    so `message_id` is what scopes it — a clock never does.
    """

    token: str
    action: str
    entity_id: str
    owner_id: int
    chat_id: int
    message_id: int
    mutation: bool


class CallbackStatePort(Protocol):
    """Durable callback meaning, addressed by an unguessable key.

    Deliberately synchronous, unlike :class:`~remote_agents.ports.session_store.SessionStore`.
    Not every caller is synchronous — several sit inside `async` handlers that already await a
    terminal — but the renderers that mint tokens are plain functions with no other reason to
    be awaitable, so making this awaitable would colour them for single-row primary-key reads
    against a local file.
    """

    def create(
        self,
        action: str,
        entity_id: str,
        owner_id: int,
        chat_id: int,
        message_id: int,
        *,
        mutation: bool = False,
    ) -> str: ...
    def resolve(
        self, token: str, *, owner_id: int, chat_id: int, message_id: int
    ) -> CallbackState | None: ...
    def claim_mutation(
        self, token: str, *, owner_id: int, chat_id: int, message_id: int
    ) -> bool: ...
    def confirm_force(
        self, token: str, *, owner_id: int, chat_id: int, message_id: int
    ) -> bool: ...
    def force_confirmed(
        self, token: str, *, owner_id: int, chat_id: int, message_id: int
    ) -> bool: ...
    def prune_for_message(self, chat_id: int, message_id: int) -> int: ...
    def active_count(self) -> int: ...
