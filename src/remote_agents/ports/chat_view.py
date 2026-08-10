"""Technology-neutral contract for the one message a control-surface chat holds."""

from __future__ import annotations

from typing import Protocol


class ChatViewPort(Protocol):
    """Which message a chat's live view currently is.

    Separate from :class:`~remote_agents.ports.callback_state.CallbackStatePort` on purpose:
    that one answers *what a token means*, addressed by an unguessable key, while this
    answers *where the screen is*, addressed by the chat. They are stored side by side and
    minted in the same migration, but a port that answered both would be two contracts
    sharing a name.

    Synchronous for the same reason its sibling is: a single-row primary-key read against a
    local file, called from renderers that have no other reason to be awaitable.
    """

    def anchor(self, chat_id: int) -> int | None: ...
    def record_anchor(self, chat_id: int, message_id: int) -> None: ...
