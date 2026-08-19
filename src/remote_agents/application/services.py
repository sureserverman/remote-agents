"""Application use cases driven only by typed ports and domain rules."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from remote_agents.application.commands import (
    AnswerTrustCommand,
    CleanupCommand,
    ForceStopCommand,
    GracefulStopCommand,
    InspectQuery,
    LaunchCommand,
    RemoteControlCommand,
    ResumeCommand,
)
from remote_agents.application.errors import (
    DuplicateCommandError,
    SessionNotFoundError,
    StopNotPermittedError,
)
from remote_agents.application.reconcile import SessionLocks
from remote_agents.application.session_actions import (
    CLEANUP,
    FORCE,
    GRACEFUL_TIMEOUT,
    UNKNOWN_SESSION,
    available_actions,
)
from remote_agents.domain.models import (
    ProfileId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.domain.state_machine import LifecycleEvent, transition
from remote_agents.domain.trust import TRUST_ANSWERABLE, TrustState
from remote_agents.ports.session_store import ProjectUsage, SessionStore
from remote_agents.ports.terminal import TerminalObservation, TerminalPort

_LOG = logging.getLogger(__name__)

#: How long a stop will wait for the console to step out of the way before destroying anyway.
#: Short on purpose, and for DEC-030's reason applied to a different caller: a console that has
#: not answered in this long is wedged, and waiting on it is how a display takes a stop with it.
#: The cost of giving up early is a dead pane left in the console's slot, which the next sync
#: clears; the cost of waiting is an agent that cannot be stopped.
_CONSOLE_HIDE_TIMEOUT_SECONDS = 2.0

#: What each non-preserving graceful stop is recorded as in the durable history (DEC-022).
#: Enumerated rather than "`unknown_session`, else a timeout", because the two are different
#: claims about the same field and an `else` makes the weaker one a silent default. The
#: exhaustiveness of this mapping rests on `TmuxRuntime.graceful_stop` emitting exactly these
#: two details, which is a fact living in another file with nothing tying them together — so a
#: third detail must land somewhere that says so rather than somewhere that guesses.
_STOP_EVENTS: dict[str, LifecycleEvent] = {
    UNKNOWN_SESSION: LifecycleEvent.GRACEFUL_STOP_NEVER_SENT,
    GRACEFUL_TIMEOUT: LifecycleEvent.GRACEFUL_STOP_TIMED_OUT,
}


@dataclass(frozen=True, slots=True)
class ResumeOutcome:
    """A resume's record, and whether this call is what created it.

    `created` cannot be derived from `record`. A conversation already bound to a RUNNING
    session and a resume that has just come up both answer RUNNING, so a surface reading only
    the record has no way to tell "I started this" from "this already existed and I started
    nothing" — and both surfaces reported the second as "Session resumed".

    It is a separate field rather than a sentinel state because the honest answer is about
    *this call*, not about the session: the same record is a correct "created" answer to the
    call that made it and a correct "already attached" answer to every call after.
    """

    record: SessionRecord
    created: bool


class SessionService:
    """Typed operations whose liveness authority is always TerminalPort."""

    def __init__(
        self,
        store: SessionStore,
        terminal: TerminalPort,
        *,
        locks: SessionLocks | None = None,
        hide_in_console: Callable[[SessionId], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._terminal = terminal
        self._locks = locks or SessionLocks()
        # Optional, on DEC-007's widening pattern: a host that wires nothing keeps the
        # destruction contract exactly as it was. Wired, it is called immediately before a
        # pane is destroyed, so the console is never asked to lose a pane standing in its own
        # window — which under the swap model is where a displayed agent lives.
        self._hide_in_console = hide_in_console

    async def launch(self, command: LaunchCommand) -> SessionRecord:
        async with self._locks.operation():
            if not await self._store.claim_idempotency_key(command.idempotency_key):
                raise DuplicateCommandError("launch callback was already handled")
            session_id = SessionId.new()
            sequence = await self._store.next_sequence(command.project_id, command.profile_id)
            record = SessionRecord(
                session_id,
                command.project_id,
                command.profile_id,
                SessionDisplayIdentity(
                    str(command.project_id),
                    str(command.profile_id),
                    "regular",
                    sequence,
                    command.label,
                ),
                SessionState.STARTING,
                datetime.now(UTC),
            )
            await self._store.save(record)
            # From here the session id exists, so the per-session lock can be taken -- and
            # must be. `ReconciliationService.session_is_busy` answers "is a caller mid-flight
            # on this session", and it can only answer from a held lock. Without this, a
            # launch is invisible to it for its whole duration: the reconciler falls back to
            # the settle window, and a launch slower than that window (a cold agent CLI) is
            # reconciled to FAILED underneath itself. The launch's own `record_event` then
            # runs `transition(FAILED, STARTUP_ERROR)`, which is not in the matrix, and raises
            # the same InvalidTransition class this stage exists to close. Found by the Stage
            # 4 gate review; the stop half was fixed first because that is the half production
            # actually hit, a stop being routinely slower than the window and a launch rarely.
            async with self._locks.for_session(session_id):
                observation = await self._terminal.launch(
                    session_id, command.project_id, command.profile_id
                )
                event = LifecycleEvent.READY if observation.live else LifecycleEvent.STARTUP_ERROR
                return await self._store.record_event(session_id, event)

    async def resume(self, command: ResumeCommand) -> ResumeOutcome:
        """Create one managed identity for a server-resolved provider conversation."""
        async with self._locks.operation():
            async with self._locks.for_conversation(
                command.profile_id, command.conversation.provider_conversation_id
            ):
                return await self._resume_locked(command)

    async def _resume_locked(self, command: ResumeCommand) -> ResumeOutcome:
        """Create or return a durable resume identity while its conversation lock is held.

        Returns **what it did**, not only what it found. A surface cannot recover that from
        the record: an already-bound RUNNING session and a resume that has just succeeded are
        the same state, so a caller branching on `record.state` alone reported "Session
        resumed" over a live session it had not touched. That was invisible for a stage
        because every state the earlier tests covered — PRESERVED, ENDED, STOP_REQUESTED,
        ORPHANED — happens to be one a *new* resume never returns, so the ambiguous pair was
        exactly the pair nothing exercised.
        """
        source_id = command.conversation.provider_conversation_id.value
        existing = await self._store.get_by_resume_source(command.profile_id, source_id)
        if existing is not None:
            return ResumeOutcome(existing, created=False)
        if not await self._store.claim_idempotency_key(command.idempotency_key):
            raise DuplicateCommandError("resume callback was already handled")
        session_id = SessionId.new()
        sequence = await self._store.next_sequence(command.project_id, command.profile_id)
        record = SessionRecord(
            session_id,
            command.project_id,
            command.profile_id,
            SessionDisplayIdentity(
                str(command.project_id), str(command.profile_id), "resumed", sequence
            ),
            SessionState.STARTING,
            datetime.now(UTC),
            command.conversation.summary.profile_id,
            source_id,
        )
        await self._store.save(record)
        # The same shape as `launch`, and owed for the same reason: the id exists from here,
        # so the per-session lock is what makes this resume visible to
        # `ReconciliationService.session_is_busy` for its whole duration.
        async with self._locks.for_session(session_id):
            observation = await self._terminal.resume(
                session_id,
                command.project_id,
                command.profile_id,
                command.conversation.provider_conversation_id,
            )
            event = LifecycleEvent.READY if observation.live else LifecycleEvent.STARTUP_ERROR
            return ResumeOutcome(await self._store.record_event(session_id, event), created=True)

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return tuple(await self._store.list())

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        """Promote only failed launches whose owned panes now show readiness evidence."""
        async with self._locks.operation():
            records = tuple(await self._store.list())
            for record in records:
                if record.state is not SessionState.FAILED:
                    continue
                async with self._locks.for_session(record.session_id):
                    current = await self._require_session(record.session_id)
                    if current.state is not SessionState.FAILED:
                        continue
                    observation = await self._terminal.confirm_ready(
                        current.session_id, current.profile_id
                    )
                    if observation.live:
                        await self._store.record_event(current.session_id, LifecycleEvent.READY)
            return tuple(await self._store.list())

    async def rename(self, session_id: SessionId, label: str | None) -> SessionRecord:
        """Name a running session, or clear its name. Metadata only — nothing is signalled.

        Under the session lock like every other mutation of a record, so a rename cannot
        interleave with a stop walking the same row to ENDED. It deliberately does not check
        the state: naming an ended session is harmless and the list still shows it until
        reconciliation removes it, so refusing would be a rule with nothing behind it.
        """
        async with self._locks.for_session(session_id):
            await self._require_session(session_id)
            return await self._store.set_label(session_id, label)

    async def project_usage(self) -> Sequence[ProjectUsage]:
        """Per-project launch history, for ordering the pickers by what is actually used.

        A pass-through to the store rather than a computation: the ranking is a pure function
        the caller applies, and putting the decay here would tie the order to whoever asked
        instead of to the screen being drawn.
        """
        return await self._store.project_usage()

    async def inspect(self, query: InspectQuery) -> TerminalObservation | None:
        return await self._terminal.inspect(query.session_id)

    async def copy_attach(self, session_id: SessionId) -> str | None:
        """Return a copyable command only after current record and terminal ownership agree.

        A **preserved** pane qualifies alongside a live one (DEC-021), and it is the terminal
        that decides which command that earns — read-only for preserved. This layer's job is
        unchanged and is the half that matters here: the ownership check. A pane whose project
        or profile disagrees with the record is refused whether it is live or preserved, so
        widening liveness does not widen *whose* pane may be handed over.
        """
        record = await self._require_session(session_id)
        observation = await self._terminal.inspect(session_id)
        if (
            observation is None
            or not (observation.live or observation.preserved)
            or observation.project_id != record.project_id
            or observation.profile_id != record.profile_id
        ):
            return None
        return await self._terminal.copy_attach(session_id)

    async def set_remote_control(self, command: RemoteControlCommand) -> RemoteControlState:
        """Execute a profile-owned state transition only once for an exact Claude session."""
        async with self._locks.operation(), self._locks.for_session(command.session_id):
            record = await self._require_session(command.session_id)
            if record.profile_id != ProfileId("claude"):
                raise ValueError("remote control is available only for Claude")
            if not await self._store.claim_idempotency_key(command.idempotency_key):
                raise DuplicateCommandError("remote control callback was already handled")
            observed = await self._terminal.remote_control(
                command.session_id, command.desired_state
            )
            # Persisted so a surface can hide the action that would do nothing. Inside the
            # session lock, after the pane has answered, and never before: recording the
            # *desired* state would claim an outcome the terminal had not reported yet, and
            # a toggle that fails would leave the record asserting a pane it never changed.
            await self._store.set_remote_control_state(command.session_id, observed)
            return observed

    async def trust_state(self, session_id: SessionId) -> TrustState:
        """Read whether this session's pane is waiting on the folder-trust question.

        Read-only and unlocked, like the other read paths: it answers a question about a
        pane right now, and holding the operation lock to do so would let a surface
        rendering a list block a stop the owner is trying to issue.
        """
        return await self._terminal.trust_state(session_id)

    async def answer_trust(self, command: AnswerTrustCommand) -> TrustState:
        """Answer the folder-trust question once for an exact Claude session.

        The profile is re-checked here rather than trusted from the caller, exactly as
        `set_remote_control` does, so a surface that offers the row on a stale observation
        still cannot drive a non-Claude pane. The *pane* half is re-checked one layer
        further down, in `answer_trust` on the terminal, because only the terminal can see
        whether the dialog is still on screen.
        """
        async with self._locks.operation(), self._locks.for_session(command.session_id):
            record = await self._require_session(command.session_id)
            if record.profile_id not in TRUST_ANSWERABLE:
                raise ValueError("folder trust is available only for Claude")
            if not await self._store.claim_idempotency_key(command.idempotency_key):
                raise DuplicateCommandError("trust callback was already handled")
            return await self._terminal.answer_trust(command.session_id)

    async def graceful_stop(self, command: GracefulStopCommand) -> TerminalObservation:
        """Stop the agent on its own terms and remove its pane in the same operation.

        A stop the owner asked for ends the session: PRESERVED is passed through so the
        recorded history still says the pane exited before it was removed, but the owner
        is not asked to close it in a second confirmed step. The cost is deliberate — the
        pane's output stops being capturable here, so nothing can be inspected after a
        graceful stop. A pane that dies on its own still lands in PRESERVED and stays
        there (RECONCILED_PANE_DEAD), which is the path that keeps output to read.

        The returned observation describes the *stop*, not what survives it: `preserved`
        means the profile's own exit sequence worked, and remains the way a caller tells
        a clean exit from `graceful_timeout`. On timeout nothing is removed and the
        session returns to RUNNING, so force stop stays a separately chosen action.

        A cleanup that fails raises with the session left in PRESERVED, where Clean up is
        still offered — a stop reported as complete over a pane still holding a terminal
        would be the worse answer.
        """
        async with self._locks.operation(), self._locks.for_session(command.session_id):
            record = await self._require_session(command.session_id)
            transition(record.state, LifecycleEvent.GRACEFUL_STOP_REQUESTED)
            await self._store.record_event(
                command.session_id, LifecycleEvent.GRACEFUL_STOP_REQUESTED
            )
            observation = await self._terminal.graceful_stop(command.session_id, command.profile_id)
            if not observation.preserved:
                # Two causes, two events (DEC-022). `unknown_session` means the terminal never
                # matched the session to a live pane it owns, so no exit sequence left this
                # host and nothing was waited for; recording a timeout for it made the durable
                # history assert something that did not happen.
                #
                # A detail neither branch knows falls back to the timeout — which is what this
                # method recorded for every cause before DEC-022, so the fallback introduces no
                # claim that was not already being made — but it says so out loud.
                #
                # **`stop_failure` resolves the same unknown the other way, and the difference
                # is not an inconsistency.** There, the choice is between "a failure" and "a
                # success", and an unknown plainly belongs on the failure side. Here both
                # answers are equally specific *claims* about what happened — one asserts a
                # wait, the other asserts that nothing left the host — so there is no
                # conservative side to fall to, and picking either silently would be inventing
                # evidence. Hence the warning: the fallback keeps the pre-DEC-022 behaviour and
                # the log is what makes a new cause somebody's problem instead of nobody's.
                event = _STOP_EVENTS.get(observation.detail)
                if event is None:
                    _LOG.warning(
                        "graceful stop reported %r, which is not a cause this records an event "
                        "for; logging it as a timeout, which may overstate what happened",
                        observation.detail,
                    )
                    event = LifecycleEvent.GRACEFUL_STOP_TIMED_OUT
                await self._store.record_event(command.session_id, event)
                return observation
            await self._store.record_event(command.session_id, LifecycleEvent.PANE_EXITED)
            await self._leave_the_console(command.session_id)
            await self._terminal.cleanup(command.session_id)
            await self._store.record_event(command.session_id, LifecycleEvent.CLEANUP_CONFIRMED)
            return observation

    async def cleanup(self, command: CleanupCommand) -> None:
        """Discard a preserved pane, refusing any state the policy does not offer it from.

        The policy check is not redundant with the `transition` below, and the gap it covers
        long predates DEC-020: `CLEANUP_CONFIRMED` is domain-legal from RUNNING and
        STOP_REQUESTED, while `available_actions` offers cleanup **only** from PRESERVED. So
        for two states the matrix would happily walk a live session to ENDED and ask the
        terminal to kill its tmux session, on an action no surface offers and no confirmation
        guards. Both surfaces gate on `available_actions` before calling, so this was not
        reachable by an owner — it was simply undefended at the layer that owns the action.

        Found by the Stage 4 gate's adversarial pass, which noticed that the sibling guard in
        `force_stop` had been justified by the claim that no such gap existed.
        """
        async with self._locks.operation(), self._locks.for_session(command.session_id):
            record = await self._require_session(command.session_id)
            if CLEANUP not in available_actions(record.state, record.orphan_provenance):
                raise StopNotPermittedError(
                    f"cleanup is not offered for a session in {record.state.value}: it "
                    "discards a preserved pane, and there is none to discard here"
                )
            transition(record.state, LifecycleEvent.CLEANUP_CONFIRMED)
            # The third destructive path, and the one the first draft of this task missed.
            # It removes a pane through the same call `graceful_stop` uses, and it is offered
            # from PRESERVED — a state whose pane is still shown (DEC-021/DEC-039), and which
            # Sub-plan 3 will let the owner display. "Every stop path" has to include the one
            # that only discards.
            await self._leave_the_console(command.session_id)
            await self._terminal.cleanup(command.session_id)
            await self._store.record_event(command.session_id, LifecycleEvent.CLEANUP_CONFIRMED)

    async def force_stop(self, command: ForceStopCommand) -> TerminalObservation:
        """End the session, and hand back what the terminal actually observed.

        **`VERIFIED_FORCE_STOP` is written whether or not a kill happened, and the event name
        overstates the one case where it did not.** When no managed pane matches, the terminal
        reports `ownership_lost` and kills nothing — and this still records the event, so the
        record reaches ENDED and the row clears. That is DEC-017, chosen deliberately: a row
        the owner cannot clear is a worse failure than an over-confident message, and failing
        closed would strand the destroyed-outside-the-app case with no way to retire it.

        DEC-017's accepted cost 1 is exactly this line. The durable history cannot distinguish
        a kill that happened from one that did not; only the **returned observation** carries
        that, which is why this method returns it rather than `None`, and why both surfaces
        read it through `session_actions.force_stop_failure`. Anyone reconstructing
        what happened from `session_events` alone will over-read this event, and there is no
        fix for that short of reopening the decision.

        Stated here because this is where a reader chasing the audit log lands. The sibling
        `graceful_stop` above splits its two causes into two events (DEC-022) — a contrast that
        otherwise invites the inference that force has only one cause. It has two; they are
        just not distinguishable *here*.
        """
        async with self._locks.operation(), self._locks.for_session(command.session_id):
            record = await self._require_session(command.session_id)
            # Defence in depth for the one refusal the matrix below cannot make. Availability
            # has always narrowed the domain on `SessionState` alone, so until DEC-020 every
            # stop the policy refused the domain refused too and this line would have been
            # dead. DEC-020 branches on a *record field*, and `transition` is a pure function
            # of state — so from here on the policy is the only thing standing between a
            # caller and a kill on a muddled-evidence ORPHANED record. That is a guard worth
            # having at the layer that owns the destructive action, not only at the two
            # surfaces that happen to call it.
            #
            # Asked through `available_actions` rather than restated: one authority on what
            # may be stopped (DEC-001), so this cannot drift away from what the surfaces
            # render. Scoped to ORPHANED so every other state keeps failing exactly as it
            # did, through the matrix, with the exception its callers already expect.
            if record.state is SessionState.ORPHANED and FORCE not in available_actions(
                record.state, record.orphan_provenance
            ):
                raise StopNotPermittedError(
                    "a force stop is not offered for this session: its pane evidence was "
                    "ambiguous, so nothing here identifies what would be killed"
                )
            transition(record.state, LifecycleEvent.VERIFIED_FORCE_STOP)
            await self._leave_the_console(command.session_id)
            observation = await self._terminal.force_stop(command.session_id)
            await self._store.record_event(command.session_id, LifecycleEvent.VERIFIED_FORCE_STOP)
            return observation

    async def _leave_the_console(self, session_id: SessionId) -> None:
        """Put the projects surface back if this session is the one being displayed.

        Immediately before the destructive call, and never at the top of a stop: a graceful
        stop that times out leaves the session running, and hiding it then would pull the
        agent out of view for a stop that did not happen.

        **Failure is swallowed here, not only in the composer.** This is the method that must
        not raise: a stop that failed because a *display* could not be rearranged is precisely
        the coupling DEC-006 forbids, and a broken console is supposed to cost the owner the
        arrangement and nothing else. The composer already degrades; this is the belt to that
        braces, placed where the lifecycle guarantee lives rather than where the presentation
        one does.

        **And the wait is bounded, which is the half that swallowing exceptions does not
        cover.** `AsyncTmuxRunner.run` awaits `process.communicate()` with no timeout — the
        hazard DEC-030 already names in this codebase — so a wedged tmux server makes a
        console round trip block rather than fail. An unbounded await here sits between
        `VERIFIED_FORCE_STOP` and the kill: the force stop on a runaway agent would never
        reach it, the per-session lock would stay held, and every other composer operation
        would queue behind the same `_links` lock. "Degrades to nothing" has to mean *within
        a bounded time*, or a display that merely stops answering takes a stop with it.
        """
        if self._hide_in_console is None:
            return
        try:
            await asyncio.wait_for(
                self._hide_in_console(session_id), timeout=_CONSOLE_HIDE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            _LOG.warning(
                "the console did not answer within %ss; stopping without rearranging it",
                _CONSOLE_HIDE_TIMEOUT_SECONDS,
            )
        except Exception:
            _LOG.exception("the console could not be rearranged before the stop; stopping anyway")

    async def _require_session(self, session_id: SessionId) -> SessionRecord:
        record = await self._store.get(session_id)
        if record is None:
            raise SessionNotFoundError(str(session_id))
        return record
