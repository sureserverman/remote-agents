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

    `record_anchor` moves the anchor unconditionally — a render knows where the screen went.
    `adopt_anchor` records one only if the chat has none; it is a separate operation because
    it must be **one statement**. Reading the anchor and then conditionally writing it is the
    read-then-write shape DEC-005 forbids on this store: two writers both see no anchor, both
    write, and the later one wins a decision it never made.

    `adopt_anchor` returns True for **the call that performed the insert, and no other** —
    not for a later call that happens to name the anchor already stored. The narrower answer
    is the one both implementations can give: SQLite's `ON CONFLICT DO NOTHING` reports
    whether a row was written and cannot distinguish which value it conflicted with.
    """

    def anchor(self, chat_id: int) -> int | None: ...
    def record_anchor(self, chat_id: int, message_id: int) -> None: ...
    def adopt_anchor(self, chat_id: int, message_id: int) -> bool: ...
