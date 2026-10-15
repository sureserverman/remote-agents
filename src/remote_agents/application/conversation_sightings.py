"""Hand each drained conversation sighting to the ledger the resume after a restart reads."""

from __future__ import annotations

from collections.abc import Iterable

from remote_agents.ports.agent_activity import AgentActivity
from remote_agents.ports.conversation_ledger import ConversationLedger


async def record_conversations(
    activities: Iterable[AgentActivity], ledger: ConversationLedger
) -> None:
    """Record every activity that names its session's own conversation.

    Only the drain loop calls this, once per pass, over what the spool yielded. The ledger keeps
    the newest sighting by instant, so the order activities arrive in does not matter.
    """
    for activity in activities:
        if activity.conversation is not None:
            await ledger.record(
                activity.session_id, activity.conversation, seen_at=activity.observed_at
            )
