"""Technology-neutral contract for the one notification a session owns in a chat."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from remote_agents.ports.agent_activity import AgentActivity


@dataclass(frozen=True, slots=True)
class StandingNotification:
    """The one message a session owns in the chat, and what it currently says.

    A session gets a message, not a stream of them. New news re-renders this one rather than
    arriving beside it, so a session that reports for eight hours occupies one slot in the
    chat instead of ninety-six -- which is the whole point, and is what per-pass grouping
    could never reach: two turns thirty minutes apart are in different passes by definition.

    `activities` is what the message spells out, kept so the re-render can carry the whole
    story rather than only the newest arrival. Without it an edit would replace "finished,
    then asked a question" with "asked a question", silently deleting agent output that the
    drain has already removed from disk.

    `token` is the callback the message's button carries. An amendment leaves both it and
    `message_id` exactly where they are -- the message never moved -- while a replacement moves
    the token onto the new message with `rebind` rather than minting a fresh one, so a session
    reporting all day adds one row to a size-bounded store instead of one per report -- the
    same reason `LiveView.move_to_bottom` rebinds rather than re-mints.
    """

    session_id: str
    message_id: int
    activities: tuple[AgentActivity, ...]
    token: str


class StandingNotificationPort(Protocol):
    """Which message a session's notification is, and what that message says.

    Synchronous for its siblings' reason (:class:`~remote_agents.ports.chat_view.ChatViewPort`,
    :class:`~remote_agents.ports.callback_state.CallbackStatePort`): primary-key reads against a
    local file, called from a delivery pass that has no other reason to await them.

    **Durable, and that durability is the contract rather than a bonus.** This was process
    memory, and the failure it produced is specific and was reported from the chat: the service
    restarted at 21:23, the session it had already sent a notification about reported again at
    21:35, and the notifier -- having forgotten which message that session owned -- sent a
    *second* one. The first stayed where it was, above a live view whose own re-send could not
    move it (`LiveView._last_arguments` is process-local too), so the owner was left with one
    notification above the menu and one below for a single session.

    It is deliberately **not** the durable queue DEC-026 declined. That one was a backlog to
    drain, bound and reason about forever; this is one row per session naming a message that
    already exists in the chat, and it answers a question -- *does this session already have a
    notification?* -- that has exactly one correct answer regardless of how many processes have
    served the chat since. The undelivered queue and the rate windows stay in memory.
    """

    def standing(self, chat_id: int) -> tuple[StandingNotification, ...]: ...
    def notification(self, chat_id: int, session_id: str) -> StandingNotification | None: ...
    def record(self, chat_id: int, notification: StandingNotification) -> None: ...
    def forget(self, chat_id: int, session_id: str) -> None: ...
