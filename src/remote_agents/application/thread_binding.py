"""Bind a fresh session's thread, by typing one line into its own pane once it is ready.

A provider whose tool shells share a daemon cannot say which session a fresh thread belongs to
(DEC-128). So a launch asks for a binding before its pane starts, and this pass mints a nonce and
sends the one line that spends it, once the pane sits on an empty idle composer. The shell that
runs the line knows its thread, and the store ties the two.

It is a pass, not a launch step, for three reasons. The console launches in another process and
only `serve` types (DEC-115). A session on its folder-trust dialog is not RUNNING, so nothing is
typed until the owner answers. And a restart finds the pending file and carries on.

A line is sent at most once per service run, because a double submit is worse than an unconfirmed
one (DEC-099). A restart that finds a nonce already minted cannot know whether it was typed, so it
tries once more with a fresh nonce; the first one is then dead, and a thread already bound is never
typed to.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from remote_agents.application.session_actions import notifiable
from remote_agents.domain.models import ProfileId, SessionId, SessionRecord, SessionState
from remote_agents.ports.terminal import PromptDelivery, PromptOutcome
from remote_agents.ports.thread_binding import PendingBinding, ThreadBindings

_LOG = logging.getLogger(__name__)

#: How long a session may go without an empty idle composer. A cold agent, or an owner typing.
COMPOSER_PATIENCE_SECONDS = 60.0
#: How long the binding may take to appear once the line was sent.
SEEN_PATIENCE_SECONDS = 20.0
#: How often a caller waiting on one binding looks again.
_WAIT_SECONDS = 1.0


class BindStep(Enum):
    """What one look at a session found."""

    BOUND = "bound"
    WAITING = "waiting"
    GAVE_UP = "gave_up"
    """It gave up on this look, so the caller decides who is told."""
    OVER = "over"
    """Nothing more will happen: it never asked, or gave up on an earlier look."""


@dataclass(slots=True)
class _Attempt:
    started_at: datetime
    retry: bool
    nonce: str | None = None
    sent_at: datetime | None = None
    over: bool = False


class ThreadBinder:
    """One pass over live sessions that asked for a binding. Everything outside is handed in."""

    def __init__(
        self,
        bindings: ThreadBindings,
        *,
        sessions: Callable[[], Awaitable[Sequence[SessionRecord]]],
        hooks: Mapping[str, str],
        send_line: Callable[[SessionId, str], Awaitable[PromptDelivery]],
        line_for: Callable[[str, str], str],
        notify: Callable[[SessionRecord], Awaitable[None]] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """`hooks` maps each profile with a thread variable to the hook name of its roster."""
        self._bindings = bindings
        self._sessions = sessions
        self._hooks = dict(hooks)
        self._send_line = send_line
        self._line_for = line_for
        self._notify = notify
        self._now = now
        self._sleep = sleep
        self._attempts: dict[str, _Attempt] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def run_once(self) -> None:
        records = await self._sessions()
        # From a list that was read: an empty stand-in for a failed read would forget everyone.
        live = {str(record.session_id) for record in records if notifiable(record.state)}
        await asyncio.to_thread(self._bindings.prune, live)
        for key in set(self._attempts) - live:
            del self._attempts[key]
            self._locks.pop(key, None)
        for record in records:
            if record.state is not SessionState.RUNNING or record.resume_source_id is not None:
                continue
            step = await self._step(record.session_id, record.profile_id)
            if step is BindStep.GAVE_UP and self._notify is not None:
                try:
                    await self._notify(record)
                except Exception:
                    _LOG.exception("the unbound notice for %s could not be sent", record.session_id)

    async def wait_bound(self, session_id: SessionId, profile_id: ProfileId) -> bool:
        """Whether this session may be typed its handoff: bound, or a provider that needs none.

        The caller reports a failure itself, so a session given up here gets no second notice.
        """
        if str(profile_id) not in self._hooks:
            return True
        while True:
            step = await self._step(session_id, profile_id)
            if step is BindStep.BOUND:
                return True
            if step is not BindStep.WAITING:
                return False
            await self._sleep(_WAIT_SECONDS)

    async def _step(self, session_id: SessionId, profile_id: ProfileId) -> BindStep:
        hook = self._hooks.get(str(profile_id))
        if hook is None:
            return BindStep.OVER
        key = str(session_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            if await asyncio.to_thread(self._bindings.names, key, hook):
                return BindStep.BOUND
            attempt = self._attempts.get(key)
            if attempt is not None and attempt.over:
                return BindStep.OVER
            state = await asyncio.to_thread(self._bindings.state, key)
            if attempt is None:
                if state in (PendingBinding.NONE, PendingBinding.GAVE_UP):
                    return BindStep.OVER
                if state is PendingBinding.UNTRUSTED:
                    return await self._give_up(key, _Attempt(self._now(), False))
                attempt = _Attempt(self._now(), retry=state is not PendingBinding.WANTED)
                self._attempts[key] = attempt
                if state is PendingBinding.RETRIED:
                    return await self._give_up(key, attempt)
            elapsed = self._now() - (attempt.sent_at or attempt.started_at)
            patience = (
                SEEN_PATIENCE_SECONDS if attempt.sent_at is not None else COMPOSER_PATIENCE_SECONDS
            )
            if elapsed.total_seconds() >= patience:
                return await self._give_up(key, attempt)
            if attempt.sent_at is None:
                await self._send(session_id, attempt)
            return BindStep.WAITING

    async def _send(self, session_id: SessionId, attempt: _Attempt) -> None:
        key = str(session_id)
        try:
            if attempt.nonce is None:
                attempt.nonce = await asyncio.to_thread(
                    self._bindings.mint, key, retry=attempt.retry
                )
            delivery = await self._send_line(session_id, self._line_for(key, attempt.nonce))
        except Exception:
            # Unknown whether it landed, so it is never sent again.
            _LOG.exception("binding %s failed partway", session_id)
            attempt.sent_at = self._now()
            return
        if delivery.outcome is not PromptOutcome.REFUSED:
            attempt.sent_at = self._now()

    async def _give_up(self, key: str, attempt: _Attempt) -> BindStep:
        attempt.over = True
        self._attempts[key] = attempt
        try:
            await asyncio.to_thread(self._bindings.give_up, key)
        except Exception:
            _LOG.exception("could not record that %s gave up its binding", key)
        return BindStep.GAVE_UP
