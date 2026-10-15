"""Each managed session's latest provider conversation, as its own agent last reported it.

A fresh launch never pins the agent's conversation id, so until this existed nothing recorded
which conversation a session was in -- only a resumed session carried one, its
`resume_source_id`, and that goes stale the moment the agent starts a new conversation in the
same pane. The ledger is written from the agent's own turn-ending hooks and read once, by the
resume-after-restart pass, which reopens exactly that conversation.

Its own table, never a field on `SessionRecord`, whose rebuild is positional (DEC-115's reason).
It holds an identifier and a time and nothing typed (DEC-067: admitted because the resume uses
it). An id that is not shaped like one is dropped, never stored: a different process wrote it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class ConversationLedger(Protocol):
    async def record(self, session_id: str, conversation_id: str, *, seen_at: datetime) -> None:
        """Keep `conversation_id` as the session's latest, unless a newer sighting is already
        kept; never raises on a malformed id, which is dropped."""
        ...

    async def latest(self, session_id: str) -> str | None:
        """The session's latest recorded conversation, or `None` if none was ever recorded."""
        ...
