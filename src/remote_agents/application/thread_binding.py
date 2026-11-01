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
from remote_agents.ports.terminal import PromptDelivery, PromptOutcome, PromptReason
from remote_agents.ports.thread_binding import PendingBinding, ThreadBindings

_LOG = logging.getLogger(__name__)

#: How long a session may go without an empty idle composer. A cold agent, or an owner typing.
COMPOSER_PATIENCE_SECONDS = 60.0
#: How long the binding may take to appear once the line was sent.
SEEN_PATIENCE_SECONDS = 20.0
#: The states a session can still be bound in, which are the states the hook roster lists.
LIVE_STATES = tuple(state for state in SessionState if notifiable(state))
#: How often a caller waiting on one binding looks again.
_WAIT_SECONDS = 1.0


def unbound_notice(record: SessionRecord, project_name: str) -> str:
    """The one sentence both surfaces say about a session that could not bind (DEC-091)."""
    return (
        f"{record.profile_id} in {project_name} #{record.display.sequence} could not bind its "
        "thread. It keeps working, but its handoffs will refuse, so it cannot roll over."
    )


class UnboundWatch:
    """Which sessions gave up their binding since this surface last looked.

    The console is another process and is not told by the pass, so it reads the store. Its first
    look only learns what is already there: a console opened later is not the moment to report an
    old failure, which the bot has said and `doctor` still counts.
    """

    def __init__(
        self,
        bindings: ThreadBindings,
        sessions: Callable[[], Awaitable[Sequence[SessionRecord]]],
    ) -> None:
        self._bindings = bindings
        self._sessions = sessions
        self._seen: set[str] | None = None

    async def newly_given_up(self) -> tuple[SessionRecord, ...]:
        given_up = []
        for record in await self._sessions():
            state = await asyncio.to_thread(self._bindings.state, str(record.session_id))
            if state is PendingBinding.GAVE_UP:
                given_up.append(record)
        keys = {str(record.session_id) for record in given_up}
        fresh = (
            ()
            if self._seen is None
            else tuple(record for record in given_up if str(record.session_id) not in self._seen)
        )
        self._seen = keys if self._seen is None else self._seen | keys
        return fresh


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
        told_elsewhere: Callable[[SessionId], Awaitable[bool]] | None = None,
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
        self._told_elsewhere = told_elsewhere
        self._now = now
        self._sleep = sleep
        self._attempts: dict[str, _Attempt] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def run_once(self) -> None:
        records = await self._sessions()
        # From a list that was read: an empty stand-in for a failed read would forget everyone.
        live = {str(record.session_id) for record in records}
        await asyncio.to_thread(self._bindings.prune, live)
        for key in set(self._attempts) - live:
            del self._attempts[key]
            self._locks.pop(key, None)
        for record in records:
            if record.state is not SessionState.RUNNING or record.resume_source_id is not None:
                continue
            key = str(record.session_id)
            if str(record.profile_id) not in self._hooks:
                continue
            # The cheap reading first: this runs every few seconds for every session, and only
            # one with a pending file or an attempt under way has anything left to do.
            if key not in self._attempts and await asyncio.to_thread(self._bindings.state, key) in (
                PendingBinding.NONE,
                PendingBinding.GAVE_UP,
            ):
                continue
            step = await self._step(record.session_id, record.profile_id)
            if step is BindStep.GAVE_UP:
                await self._tell(record)

    async def _tell(self, record: SessionRecord) -> None:
        """One notice, unless a rollover waits on this session and will report it failing."""
        if self._notify is None:
            return
        try:
            if self._told_elsewhere is None or not await self._told_elsewhere(record.session_id):
                await self._notify(record)
        except Exception:
            _LOG.exception("the unbound notice for %s could not be sent", record.session_id)

    async def wait_bound(
        self, session_id: SessionId, profile_id: ProfileId, *, tell: bool = False
    ) -> bool:
        """Whether this session may be typed its first work: bound, or a provider needing none.

        A rollover reports a failure itself, so it leaves `tell` off. A schedule still types its
        prompt into an unbound session, so it asks for the notice. Bounded here too, because a
        busy composer does not use up the pass's own patience.
        """
        if str(profile_id) not in self._hooks:
            return True
        waited = 0.0
        while waited < COMPOSER_PATIENCE_SECONDS + SEEN_PATIENCE_SECONDS:
            step = await self._step(session_id, profile_id)
            if step is BindStep.BOUND:
                return True
            if step is BindStep.GAVE_UP and tell:
                records = await self._sessions()
                for record in records:
                    if record.session_id == session_id:
                        await self._tell(record)
            if step is not BindStep.WAITING:
                return False
            await self._sleep(_WAIT_SECONDS)
            waited += _WAIT_SECONDS
        return False

    async def _step(self, session_id: SessionId, profile_id: ProfileId) -> BindStep:
        hook = self._hooks.get(str(profile_id))
        if hook is None:
            return BindStep.OVER
        key = str(session_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            if await asyncio.to_thread(self._bindings.names, key, hook):
                # Nothing left to do, so the pass goes back to its cheap reading.
                self._attempts.pop(key, None)
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
        elif delivery.reason is PromptReason.BUSY:
            # A turn the owner started can run for an hour and says nothing against the pane.
            # The thread is the same when it ends, so the patience starts again then.
            attempt.started_at = self._now()

    async def _give_up(self, key: str, attempt: _Attempt) -> BindStep:
        attempt.over = True
        self._attempts[key] = attempt
        try:
            await asyncio.to_thread(self._bindings.give_up, key)
        except Exception:
            _LOG.exception("could not record that %s gave up its binding", key)
        return BindStep.GAVE_UP
