"""Relay an owner's message into a session: typed now, queued for later, or refused (DEC-099).

The owner's rulings, in order:
- **Typed now if the agent is idle.** The terminal decides that, from a fresh capture, and types
  only into an empty idle composer (`TerminalPort.send_prompt`).
- **Otherwise queued -- one message per session, the newest replacing an older one** -- and
  delivered after that session's next "finished" event, when the idle check runs again. Only a
  refusal that waiting can resolve is queued: busy, a dialog, a composer holding text, a screen
  not recognised, another sender holding the keys.
- **An agent with no "finished" event this project drains refuses rather than queues**, because
  nothing would ever deliver the message. Which agents those are is the composition's to say
  (`queues_for`), read off what each provider installs -- never a provider's name here.
- An unconfirmed delivery is never retried: a double submit is worse than an unconfirmed one.
"""

from __future__ import annotations

from collections.abc import Callable

from remote_agents.domain.models import ProfileId, SessionId, SessionState
from remote_agents.ports.message_relay import RelayOutcome, RelayResult
from remote_agents.ports.queued_prompts import QueuedPrompt, QueuedPromptStore
from remote_agents.ports.session_store import SessionStore
from remote_agents.ports.terminal import PromptDelivery, PromptOutcome, PromptReason, TerminalPort

WAITABLE: frozenset[PromptReason] = frozenset(
    {
        PromptReason.BUSY,
        PromptReason.DIALOG,
        PromptReason.COMPOSING,
        PromptReason.UNRECOGNISED,
        PromptReason.KEYS_BUSY,
    }
)
"""The refusals a later "finished" event can resolve -- the ones worth queueing for."""


class PromptRelay:
    """Send, queue or refuse an owner's message; deliver the waiting one on "finished"."""

    def __init__(
        self,
        terminal: TerminalPort,
        queue: QueuedPromptStore,
        sessions: SessionStore,
        *,
        queues_for: Callable[[ProfileId], bool],
    ) -> None:
        self._terminal = terminal
        self._queue = queue
        self._sessions = sessions
        self._queues_for = queues_for

    async def submit(self, session_id: SessionId, text: str) -> RelayResult:
        record = await self._sessions.get(session_id)
        if record is None or record.state is not SessionState.RUNNING:
            self._queue.clear(str(session_id))
            return RelayResult(RelayOutcome.REFUSED, PromptReason.NOT_RUNNING)
        delivery = await self._terminal.send_prompt(session_id, text)
        if delivery.outcome is PromptOutcome.REFUSED:
            if delivery.reason in WAITABLE and self._queues_for(record.profile_id):
                replaced = self._queue.queue(str(session_id), text)
                return RelayResult(RelayOutcome.QUEUED, delivery.reason, replaced=replaced)
            return RelayResult(RelayOutcome.REFUSED, delivery.reason)
        # Sent, or pasted and unconfirmed: either way this newer message went to the pane, so
        # an older one still waiting must not fire after it (newest wins).
        self._queue.clear(str(session_id))
        return _result(delivery)

    async def retry(self, session_id: SessionId) -> RelayResult | None:
        prompt = self._queue.claim(str(session_id))
        if prompt is None:
            return None
        record = await self._sessions.get(session_id)
        if record is None or record.state is not SessionState.RUNNING:
            self._queue.settle(prompt)
            return RelayResult(RelayOutcome.REFUSED, PromptReason.NOT_RUNNING)
        delivery = await self._terminal.send_prompt(session_id, prompt.text)
        if delivery.outcome is PromptOutcome.REFUSED and delivery.reason in WAITABLE:
            # Still not idle: wait for the next "finished" -- unless the owner cancelled or
            # replaced it meanwhile, which `restore` answers by declining.
            self._queue.restore(prompt)
            return RelayResult(RelayOutcome.QUEUED, delivery.reason)
        self._queue.settle(prompt)
        return _result(delivery)

    def cancel(self, session_id: SessionId) -> bool:
        return self._queue.cancel(str(session_id))

    def pending(self, session_id: SessionId) -> QueuedPrompt | None:
        return self._queue.pending(str(session_id))

    async def sweep(self) -> None:
        """Drop every waiting message whose session stopped, ended or is gone."""
        for waiting in self._queue.waiting():
            record = await self._sessions.get(SessionId.parse(waiting.session_id))
            if record is None or record.state is not SessionState.RUNNING:
                self._queue.clear(waiting.session_id)


def _result(delivery: PromptDelivery) -> RelayResult:
    outcome = {
        PromptOutcome.SENT: RelayOutcome.SENT,
        PromptOutcome.REFUSED: RelayOutcome.REFUSED,
        PromptOutcome.UNCONFIRMED: RelayOutcome.UNCONFIRMED,
    }[delivery.outcome]
    return RelayResult(outcome, delivery.reason)
