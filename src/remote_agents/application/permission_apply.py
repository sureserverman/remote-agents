"""Set each launched session's permission preset on its provider's shared server.

A preset is not a launch argument (`domain.profiles.PermissionPreset`): a provider whose threads
live on a shared server takes it after launch, through `ports.thread_permissions`. So a launch
records what it asked for (`PermissionBindings.state`), its pane comes up on the provider's own
default, and this pass applies the preset once the session's thread is known.

**Which thread.** Only the session's own: the id it was resumed from, or the thread its binding
names (DEC-138). Never a thread picked by folder or by time (DEC-128, DEC-134); the pass cannot
even ask the server what is loaded.

**Which sessions.** Running ones that have a state row. A session with no row was launched with
flags by an older build and runs off the shared server, so nothing is applied to it. The
provider's own default asks for nothing and counts as applied.

**When it gives up.** One failure is retried on the next pass. A second is recorded with its
reason and stands: it is not tried again until the owner asks for a preset again or the server
is a different one. A server that restarted has forgotten the setting, so a changed server
identity re-applies an applied preset once.

It is a pass in `serve`, like the binder it follows, because only `serve` acts on panes
unattended (DEC-115) and a restart finds the state rows and carries on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from remote_agents.application.thread_binding import LIVE_STATES
from remote_agents.domain.models import ProfileId, SessionId, SessionRecord, SessionState
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.ports.permission_bindings import PermissionBindings, PermissionState
from remote_agents.ports.thread_permissions import ThreadPermissions, ThreadPermissionsError

_LOG = logging.getLogger(__name__)

#: The least time between a failed apply and its one retry: one pass of `serve`. A caller
#: waiting on a session looks more often than that, and must not spend both tries at once.
RETRY_SECONDS = 3.0
#: How long a caller about to type automatic work waits for a bound session's preset.
APPLY_PATIENCE_SECONDS = 30.0
#: How often such a caller looks again.
_WAIT_SECONDS = 1.0
_TRIES = 2

UNBOUND_REASON = "its thread could not be bound"
NOT_IN_TIME_REASON = "its permissions were not applied in time"
ENDED_REASON = "the session has ended"
#: What a caller records when it did not type because of this: a key each surface words.
PERMISSIONS_HELD = "permissions"
_UNEXPECTED_REASON = "the apply failed unexpectedly"


@dataclass(frozen=True, slots=True)
class Readiness:
    """Whether automatic work may be typed into a session, and what stands in the way."""

    bound: bool = True
    """Its thread is bound, or it needs no binding. Unbound, its handoffs would refuse."""
    unapplied: str | None = None
    """Why its preset is not on the server, so the pane runs on the provider's own default.
    None when nothing is owed. Owner-readable, and holds no secret."""


READY = Readiness()


def permissions_notice(record: SessionRecord, project_name: str, reason: str) -> str:
    """The one sentence both surfaces say about a session whose preset was not applied
    (DEC-091). `reason` is the port's own: short, and holding no secret."""
    return (
        f"{record.profile_id} in {project_name} #{record.display.sequence} could not be given "
        f"its permissions: {reason}. It runs on the agent's own configuration, and nothing "
        "automatic is typed into it."
    )


def permission_state_line(
    launched_with: PermissionPreset | None, state: PermissionState | None
) -> str | None:
    """What session inspection says about the preset on the server, on both surfaces, or None
    when there is nothing to say. `launched_with` is the launch snapshot."""
    if state is None:
        # No state row: launched before presets were applied on the server. A preset was then
        # a launch flag, which runs the pane off the shared server; the provider's own default
        # added no flag, and a session with no snapshot says nothing about its launch.
        if launched_with is None or launched_with is PermissionPreset.PROVIDER_DEFAULT:
            return None
        return "launched with flags (v0.69.0); relaunch for Remote Control"
    if state.failure is not None:
        return f"not applied: {state.failure}"
    if state.requested is PermissionPreset.PROVIDER_DEFAULT or state.applied is state.requested:
        return "applied"
    return "not applied yet"


class PermissionFailureWatch:
    """Which sessions' presets failed to apply since this surface last looked.

    The console is another process and is not told by the pass, so it reads the store, as
    `UnboundWatch` does. Its first look only learns what is already there. A session whose
    failure cleared and came back is news again.
    """

    def __init__(
        self,
        bindings: PermissionBindings,
        sessions: Callable[[], Awaitable[Sequence[SessionRecord]]],
    ) -> None:
        self._bindings = bindings
        self._sessions = sessions
        self._seen: set[str] | None = None

    async def newly_failed(self) -> tuple[tuple[SessionRecord, str], ...]:
        failed = []
        for record in await self._sessions():
            state = await self._bindings.state(record.session_id)
            if state is not None and state.failure is not None:
                failed.append((record, state.failure))
        keys = {str(record.session_id) for record, _ in failed}
        fresh = (
            ()
            if self._seen is None
            else tuple(pair for pair in failed if str(pair[0].session_id) not in self._seen)
        )
        self._seen = keys
        return fresh


class ApplyStep(Enum):
    """What one look at a session found."""

    APPLIED = "applied"
    """Nothing is owed: applied, or a session this pass never touches."""
    WAITING = "waiting"
    FAILED = "failed"
    """The failure was recorded on this look."""
    OVER = "over"
    """A failure recorded earlier stands."""


@dataclass(slots=True)
class _Attempt:
    requested: PermissionPreset
    failures: int = 0
    failed_at: datetime | None = None


class PermissionApplyPass:
    """One pass over running sessions that are owed a preset. Everything outside is handed in."""

    def __init__(
        self,
        bindings: PermissionBindings,
        *,
        sessions: Callable[[], Awaitable[Sequence[SessionRecord]]],
        appliers: Mapping[str, ThreadPermissions],
        thread_of: Callable[[str, str], str | None],
        unbound: Callable[[str], bool] | None = None,
        bound: Callable[..., Awaitable[bool]] | None = None,
        notify: Callable[[SessionRecord, str], Awaitable[None]] | None = None,
        told_elsewhere: Callable[[SessionId], Awaitable[bool]] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """`appliers` maps each profile that takes a preset after launch to its port.

        `thread_of` names a fresh session's bound thread, given the session and its profile,
        and `unbound` says its binding gave up; both read files, so they run off the loop.
        `bound` is the binder's own wait. `notify` tells the owner about a failure that now
        stands, once, unless `told_elsewhere` says another report will name it (a rollover's).
        """
        self._bindings = bindings
        self._sessions = sessions
        self._appliers = dict(appliers)
        self._thread_of = thread_of
        self._unbound = unbound
        self._bound = bound
        self._notify = notify
        self._told_elsewhere = told_elsewhere
        self._now = now
        self._sleep = sleep
        self._attempts: dict[str, _Attempt] = {}
        #: The server a standing failure was recorded under, None when it could not be read.
        self._failed_under: dict[str, str | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def tracked(self) -> frozenset[str]:
        """The sessions this pass holds anything in memory for."""
        return frozenset(self._attempts) | frozenset(self._failed_under) | frozenset(self._locks)

    async def run_once(self) -> None:
        records = await self._sessions()
        # From a list that was read: an empty stand-in for a failed read would forget everyone.
        live = {str(record.session_id) for record in records}
        for key in self.tracked - live:
            self._attempts.pop(key, None)
            self._failed_under.pop(key, None)
            self._locks.pop(key, None)
        for record in records:
            if record.state is not SessionState.RUNNING:
                continue
            if str(record.profile_id) not in self._appliers:
                continue
            try:
                await self._look(record)
            except Exception:
                # One session's store or server trouble must not cost the others their preset.
                _LOG.exception("the permission pass could not look at %s", record.session_id)

    async def wait_ready(
        self, session_id: SessionId, profile_id: ProfileId | None = None, *, tell: bool = False
    ) -> Readiness:
        """Whether this session may be typed automatic work: bound, then applied. Bounded.

        The bound is the binder's own wait, then `APPLY_PATIENCE_SECONDS` of looking again, plus
        the time the applies themselves take: at most two, each under the port's own deadline.
        A resumed session is never asked for a binding: its thread is the one it resumed.
        `tell` is the binder's: whether it reports an unbound session itself.
        """
        record = await self._record(session_id)
        profile = profile_id if profile_id is not None else getattr(record, "profile_id", None)
        if profile is None:
            return Readiness(unapplied=ENDED_REASON)
        bound = True
        if self._bound is not None and (record is None or record.resume_source_id is None):
            try:
                bound = await self._bound(session_id, profile, tell=tell)
            except Exception:
                # Unbound is the safe reading of a wait that failed, and it says nothing about
                # permissions: a provider that takes no preset is still owed none.
                _LOG.exception("waiting for the binding of %s failed", session_id)
                bound = False
        if str(profile) not in self._appliers:
            return Readiness(bound=bound)
        waited = 0.0
        while True:
            record = await self._record(session_id)
            if record is None or record.state not in LIVE_STATES:
                return Readiness(bound=bound, unapplied=ENDED_REASON)
            step = (
                await self._look(record)
                if record.state is SessionState.RUNNING
                else ApplyStep.WAITING
            )
            if step is ApplyStep.APPLIED:
                return Readiness(bound=bound)
            # Unbound, the binder has already waited: one look says whether a preset is owed.
            if step is not ApplyStep.WAITING or not bound or waited >= APPLY_PATIENCE_SECONDS:
                state = await self._bindings.state(session_id)
                failure = None if state is None else state.failure
                return Readiness(bound=bound, unapplied=failure or NOT_IN_TIME_REASON)
            await self._sleep(_WAIT_SECONDS)
            waited += _WAIT_SECONDS

    async def _record(self, session_id: SessionId) -> SessionRecord | None:
        return next(
            (seen for seen in await self._sessions() if seen.session_id == session_id), None
        )

    async def _look(self, record: SessionRecord) -> ApplyStep:
        """One look at a session, telling the owner when it found a failure that now stands."""
        step = await self._step(record)
        if step is ApplyStep.FAILED and self._notify is not None:
            try:
                elsewhere = self._told_elsewhere is not None and await self._told_elsewhere(
                    record.session_id
                )
                state = None if elsewhere else await self._bindings.state(record.session_id)
                if state is not None and state.failure is not None:
                    await self._notify(record, state.failure)
            except Exception:
                _LOG.exception("the permission notice for %s could not be sent", record.session_id)
        return step

    async def _step(self, record: SessionRecord) -> ApplyStep:
        applier = self._appliers.get(str(record.profile_id))
        if applier is None:
            return ApplyStep.APPLIED
        key = str(record.session_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            state = await self._bindings.state(record.session_id)
            if state is None or state.requested is PermissionPreset.PROVIDER_DEFAULT:
                return ApplyStep.APPLIED
            identity = await self._identity(applier)
            if state.failure is None:
                self._failed_under.pop(key, None)
                if state.applied is state.requested and identity in (None, state.daemon):
                    self._attempts.pop(key, None)
                    return ApplyStep.APPLIED
            elif key not in self._failed_under:
                # Recorded by an earlier run of the service, which told the owner then.
                self._failed_under[key] = identity
                return ApplyStep.OVER
            elif identity is None or identity == self._failed_under[key]:
                return ApplyStep.OVER
            thread = record.resume_source_id or await asyncio.to_thread(
                self._thread_of, key, str(record.profile_id)
            )
            if thread is None:
                # Not bound yet, or never asked to be. A launch that writes a state row asks
                # for a binding too, so a session that waits here for ever is one whose launch
                # was interrupted between the two, and its pane never came up.
                if self._unbound is None or not await asyncio.to_thread(self._unbound, key):
                    return ApplyStep.WAITING
                return await self._fail(record.session_id, state, UNBOUND_REASON, identity)
            attempt = self._attempts.get(key)
            if attempt is None or attempt.requested is not state.requested:
                attempt = self._attempts[key] = _Attempt(state.requested)
            if (
                attempt.failed_at is not None
                and (self._now() - attempt.failed_at).total_seconds() < RETRY_SECONDS
            ):
                return ApplyStep.WAITING
            try:
                await applier.apply(thread, state.requested)
            except ThreadPermissionsError as error:
                reason = error.reason
            except Exception:
                # Not the port's error, so not its vetted reason: the text stays in the log.
                _LOG.exception("applying permissions to %s failed unexpectedly", key)
                reason = _UNEXPECTED_REASON
            else:
                self._attempts.pop(key, None)
                self._failed_under.pop(key, None)
                await self._bindings.mark_applied(
                    record.session_id, state.requested, identity or ""
                )
                return ApplyStep.APPLIED
            attempt.failures += 1
            attempt.failed_at = self._now()
            if attempt.failures < _TRIES:
                return ApplyStep.WAITING
            return await self._fail(record.session_id, state, reason, identity)

    async def _fail(
        self, session_id: SessionId, state: PermissionState, reason: str, identity: str | None
    ) -> ApplyStep:
        """Record a failure that stands. News only when none stood before.

        `state` was read before the apply, which can run for seconds. A preset asked for since
        then does not inherit this failure: nothing is recorded and the next look starts over.
        """
        key = str(session_id)
        self._attempts.pop(key, None)
        if state.failure != reason and not await self._bindings.mark_failed(
            session_id, reason, requested=state.requested
        ):
            self._failed_under.pop(key, None)
            return ApplyStep.WAITING
        self._failed_under[key] = identity
        return ApplyStep.FAILED if state.failure is None else ApplyStep.OVER

    @staticmethod
    async def _identity(applier: ThreadPermissions) -> str | None:
        try:
            return await applier.server_identity()
        except Exception:
            _LOG.exception("the server identity could not be read")
            return None
