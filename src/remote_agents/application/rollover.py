"""Turn a workflow's `ready` envelope into one fresh successor, then stop its predecessor.

The service runs `RolloverPass.run_once` on a clock of its own, in `serve` only (DEC-115). With
the `rollover.auto_rollover` switch on, each pass walks every open rollover one step further.
Only sessions of a `rollable` profile -- one whose plan executor writes envelopes -- take part:

- a `ready` envelope naming a RUNNING rollable session opens a rollover at HANDOFF_READY;
- HANDOFF_READY waits for the predecessor's pane to be IDLE, persists SUCCESSOR_STARTING, and
  launches the predecessor's own project and profile under the key `rollover:<handoff_id>`
  (DEC-114), recording the successor the moment the launch returns;
- a RUNNING successor is typed the fixed template through the terminal's guarded send, with the
  schedules' retry policy (DEC-099), and moves to ADOPTING once the send is confirmed;
- an `accepted` envelope from that successor moves to SUCCESSOR_ACCEPTED, and only then is the
  predecessor stopped -- gracefully, against an idle pane, never by force (DEC-007).

Every state is persisted *before* the action it licenses, so a restart part-way finds the
intent and gives it up rather than repeating it (DEC-109, DEC-004): no launch is ever made from
a row that may already have launched. Each way a rollover ends short of COMPLETED, with the
code it is recorded under:

- `FAILED: predecessor-not-idle` -- the predecessor's pane was not IDLE for `NOT_IDLE_PATIENCE`
- `FAILED: predecessor-gone` -- the predecessor stopped running before its successor was up
- `FAILED: launch-failed` -- the launch raised, or answered with no session
- `FAILED: successor-unknown` -- a launch may have happened with no successor recorded
- `FAILED: successor-untrusted` -- the successor came up on its folder-trust dialog
- `FAILED: successor-failed` -- the successor failed to start, or its pane went away
- `FAILED: not-typed` -- the template could not be typed, or its send was not confirmed
- `FAILED: adoption-timeout` -- no `accepted` within `ADOPTION_PATIENCE`
- `FAILED: id-mismatch`, `FAILED: no-ready`, `FAILED: cwd-mismatch`,
  `FAILED: branch-mismatch`, `FAILED: plan-missing` -- the successor's own `failed` envelope
- `FAILED: not-rollable` -- the owner asked for a session whose agent writes no envelopes
- `FAILED: no-handoff-root` -- the owner asked for a session whose project is in no checkout
- `FAILED: request-unwritten` -- the owner's request could not be written for the workflow
- `FAILED: request-lost` -- the written request is gone, or names another session, before the
  workflow answered it; it is never written again (DEC-004)
- `STOP_FAILED` -- the graceful stop was not sent or did not verify; its typed cause is the
  code: `predecessor-not-idle` when the pane was not idle under the stop's own key lock or for
  `NOT_IDLE_PATIENCE` before it, `stop-unconfirmed` when a restart found the stop in flight.
  Force stop stays the owner's.

The predecessor is untouched by every FAILED outcome: the only call that stops it sits behind
`may_stop_predecessor`, and that stop goes only onto an idle composer, judged under the key lock
its keys are sent under -- it never interrupts a turn. At every terminal state the handoff's
envelopes are discarded and, unless another rollover of the predecessor is open, a request
naming it is withdrawn, so the directory does not fill and a later plan in the same pane does
not hand off at its first gate.

`request.json` is one file per checkout, so at most one owner's request per checkout is out at
a time: the oldest open owner-asked rollover holds it until it ends, and later ones wait.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from remote_agents.application.commands import GracefulStopCommand, LaunchCommand
from remote_agents.application.errors import DuplicateCommandError
from remote_agents.application.prompt_delivery import DeliveryVerdict, delivery_verdict
from remote_agents.application.schedules import RETRY_SECONDS, STARTUP_PATIENCE
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionRecord, SessionState
from remote_agents.domain.rollover import (
    SUCCESSOR_FAILED,
    SUCCESSOR_UNKNOWN,
    TERMINAL,
    RecoveryFacts,
    RolloverState,
    may_stop_predecessor,
    recovery_action,
)
from remote_agents.ports.handoff_envelopes import HandoffEnvelope, HandoffEnvelopes, HandoffEvent
from remote_agents.ports.rollover_store import IllegalRolloverMove, Rollover, RolloverStore
from remote_agents.ports.terminal import (
    NOT_IDLE,
    PromptDelivery,
    PromptReason,
    TerminalObservation,
)

_LOG = logging.getLogger(__name__)

_S = RolloverState

TEMPLATE = "/planning:executing-plans --adopt-handoff {handoff_id}"
"""The only text a rollover types. An envelope supplies the id, which the reader has already
held to `^h-[0-9a-f]{20}$`; it never supplies text."""

NOT_IDLE_PATIENCE = timedelta(minutes=10)
"""How long a predecessor may stay not-IDLE at a step that needs it idle before it is given up."""

ADOPTION_PATIENCE = timedelta(minutes=30)
"""How long a typed successor has to write `accepted`, counted from entering ADOPTING."""

PREDECESSOR_NOT_IDLE = "predecessor-not-idle"
PREDECESSOR_GONE = "predecessor-gone"
LAUNCH_FAILED = "launch-failed"
SUCCESSOR_UNTRUSTED = "successor-untrusted"
NOT_TYPED = "not-typed"
ADOPTION_TIMEOUT = "adoption-timeout"
STOP_UNCONFIRMED = "stop-unconfirmed"
NOT_ROLLABLE = "not-rollable"
NO_HANDOFF_ROOT = "no-handoff-root"
REQUEST_UNWRITTEN = "request-unwritten"
REQUEST_LOST = "request-lost"

#: What a booting agent shows on its way to an idle composer, so the template waits it out.
_BOOTING = frozenset(
    {DeliveryVerdict.WAIT, DeliveryVerdict.NOT_RUNNING, DeliveryVerdict.UNRECOGNISED}
)

#: A predecessor in one of these is no longer running and will not run again in this pane.
_STOPPED = frozenset({SessionState.ENDED, SessionState.PRESERVED})

#: A launched successor in one of these is typed to; the send waits out a still-booting agent,
#: as the schedules' first prompt does.
_UP = frozenset({SessionState.RUNNING, SessionState.STARTING})


@dataclass(frozen=True, slots=True)
class _Seen:
    """One pass's view: the sessions, and where each rollable project's envelopes live."""

    sessions: dict[SessionId, SessionRecord]
    roots: dict[ProjectId, Path]

    def running(self, session_id: SessionId | None) -> bool:
        record = None if session_id is None else self.sessions.get(session_id)
        return record is not None and record.state is SessionState.RUNNING

    def up(self, session_id: SessionId | None) -> bool:
        """Whether a successor is still there: running, or still recorded as starting."""
        record = None if session_id is None else self.sessions.get(session_id)
        return record is not None and record.state in _UP


class RolloverPass:
    """One pass over envelopes and open rollovers. Everything outside is handed in."""

    def __init__(
        self,
        store: RolloverStore,
        envelopes: HandoffEnvelopes,
        *,
        rollable: frozenset[ProfileId],
        sessions: Callable[[], Awaitable[Sequence[SessionRecord]]],
        handoff_root: Callable[[ProjectId], Path | None],
        enabled: Callable[[], Awaitable[bool]],
        idle: Callable[[SessionId], Awaitable[bool]],
        launch: Callable[[LaunchCommand], Awaitable[object]],
        send: Callable[[SessionId, str], Awaitable[PromptDelivery]],
        graceful_stop: Callable[[GracefulStopCommand], Awaitable[TerminalObservation]],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._envelopes = envelopes
        self._rollable = rollable
        self._sessions = sessions
        self._handoff_root = handoff_root
        self._enabled = enabled
        self._idle = idle
        self._launch = launch
        self._send = send
        self._graceful_stop = graceful_stop
        self._now = now
        self._sleep = sleep
        self._recovered = False

    async def run_once(self) -> None:
        """Walk every rollover one step, each from what is persisted now.

        With the switch off nothing is opened, launched, typed or stopped, and no row is
        written; the pass only withdraws requests no open rollover stands behind.
        """
        seen = await self._look()
        if await self._enabled():
            if not self._recovered:
                await self._note_restart(seen)
                self._recovered = True
            for project_id, root in seen.roots.items():
                try:
                    await self._pick_up_ready(project_id, root, seen)
                except Exception:
                    # One checkout's envelopes cost that checkout this pass, never the steps of
                    # rollovers already open -- an accepted one's stop among them.
                    _LOG.exception("handoffs in %s could not be read; tried next pass", root)
            for rollover in await self._store.open_rollovers():
                try:
                    await self._step(rollover, seen)
                except IllegalRolloverMove:
                    # Another writer moved it since it was read (the owner's cancel); its move
                    # stands, and the next pass reads where it now is.
                    _LOG.info("rollover %s moved under this pass; left as it is", rollover.id)
                except Exception:
                    # One rollover's fault costs that rollover this pass, never the others'.
                    # It is not marked failed: what went wrong is unknown, and every step is
                    # taken again from what is persisted.
                    _LOG.exception(
                        "rollover %s could not be walked; it is tried next pass", rollover.id
                    )
        await self._withdraw_stale_requests(seen)

    async def _looks_idle(self, session_id: SessionId) -> bool:
        """The idle look, with a look that raised read as not idle: the step then waits, and
        its patience runs, rather than failing every pass on the same fault."""
        try:
            return await self._idle(session_id)
        except Exception:
            _LOG.exception("could not look at %s; read as not idle", session_id)
            return False

    async def _note_restart(self, seen: _Seen) -> None:
        """This pass is new, so every rollover already open was left by one that is gone: say
        so on each, with what the restart table makes of it (brief §26), before it is walked.
        The walk itself agrees with the table; the row is what lets the audit see a restart."""
        for rollover in await self._store.open_rollovers():
            facts = RecoveryFacts(
                has_successor_id=rollover.successor_session_id is not None,
                successor_alive=seen.up(rollover.successor_session_id),
                timed_out=rollover.state is _S.ADOPTING
                and self._now() - rollover.updated_at >= ADOPTION_PATIENCE,
            )
            action = recovery_action(rollover.state, facts)
            await self._store.note(rollover.id, f"restart: {action.value}", at=self._now())

    async def _look(self) -> _Seen:
        sessions = {record.session_id: record for record in await self._sessions()}
        roots: dict[ProjectId, Path] = {}
        for record in sessions.values():
            if record.profile_id not in self._rollable or record.project_id in roots:
                continue
            root = self._handoff_root(record.project_id)
            if root is not None:
                roots[record.project_id] = root
        return _Seen(sessions, roots)

    # -- envelopes ---------------------------------------------------------------------------

    async def _pick_up_ready(self, project_id: ProjectId, root: Path, seen: _Seen) -> None:
        """Open, or find again, the rollover each of the project's `ready` envelopes names."""
        held = {r.handoff_id for r in await self._store.open_rollovers() if r.handoff_id}
        for handoff_id, group in _grouped(self._envelopes.events(root)).items():
            ready = group.get(HandoffEvent.READY)
            if ready is None:
                # An `accepted` or `failed` with no `ready` belongs to no rollover this
                # service can still act on -- one already finished, or a forgery.
                if handoff_id not in held:
                    self._envelopes.discard(root, handoff_id)
                continue
            predecessor = _session_id(ready.managed_session_id)
            record = None if predecessor is None else seen.sessions.get(predecessor)
            if (
                record is None
                or record.project_id != project_id
                or record.profile_id not in self._rollable
                or record.state is not SessionState.RUNNING
            ):
                continue
            rollover = await self._store.open_for_ready(
                record.session_id,
                handoff_id,
                project_id=project_id,
                profile_id=record.profile_id,
                plan=ready.plan,
                at=self._now(),
            )
            # None is not an error: another rollover is open for this predecessor, or its
            # last one failed and the `ready` waits for the owner to ask again (DEC-115).
            if rollover is not None and rollover.state in TERMINAL:
                # A replayed `ready` of a finished handoff: its envelopes go, and the
                # predecessor's request is withdrawn only if none of its rollovers is open.
                await self._finish(rollover, seen)

    def _adoption(self, rollover: Rollover, seen: _Seen) -> HandoffEnvelope | None:
        """The successor's own `accepted` or `failed` for this handoff, if it wrote one."""
        root = seen.roots.get(rollover.project_id)
        if root is None or rollover.handoff_id is None or rollover.successor_session_id is None:
            return None
        group = _grouped(self._envelopes.events(root)).get(rollover.handoff_id, {})
        for event in (HandoffEvent.ACCEPTED, HandoffEvent.FAILED):
            envelope = group.get(event)
            # A handoff id answers only for the session this service launched: an envelope
            # from any other id is ignored, never acted on (DEC-115).
            if envelope is not None and envelope.managed_session_id == str(
                rollover.successor_session_id
            ):
                return envelope
        return None

    # -- one step ----------------------------------------------------------------------------

    async def _step(self, rollover: Rollover, seen: _Seen) -> None:
        match rollover.state:
            case _S.REQUESTED:
                await self._signal(rollover, seen)
            case _S.HANDOFF_READY:
                await self._start_successor(rollover, seen)
            case _S.SUCCESSOR_STARTING:
                await self._found_starting(rollover, seen)
            case _S.ADOPTING:
                await self._await_adoption(rollover, seen)
            case _S.SUCCESSOR_ACCEPTED:
                await self._stop_predecessor(rollover, seen)
            case _S.PREDECESSOR_STOPPING:
                await self._reconcile_stop(rollover, seen)

    async def _signal(self, rollover: Rollover, seen: _Seen) -> None:
        """The owner asked: write the request once, recorded first, so a restart never re-asks
        (DEC-004). A request found gone is given up, never written again."""
        predecessor = seen.sessions.get(rollover.predecessor_session_id)
        if predecessor is None or predecessor.state is not SessionState.RUNNING:
            await self._fail(rollover, seen, PREDECESSOR_GONE)
            return
        if predecessor.profile_id not in self._rollable:
            await self._fail(rollover, seen, NOT_ROLLABLE)
            return
        root = seen.roots.get(rollover.project_id)
        if root is None:
            await self._fail(rollover, seen, NO_HANDOFF_ROOT)
            return
        if not await self._holds_the_request_slot(rollover, root, seen):
            return
        wanted = str(rollover.predecessor_session_id)
        if await self._store.record_request(rollover.id, at=self._now()):
            if not self._envelopes.write_request(root, wanted):
                await self._fail(rollover, seen, REQUEST_UNWRITTEN)
        elif self._envelopes.requested(root) != wanted:
            # Recorded as written, and not there: a crash between the record and the write, or
            # the file removed or replaced by someone else. Never written twice (DEC-004).
            await self._fail(rollover, seen, REQUEST_LOST)

    async def _holds_the_request_slot(self, rollover: Rollover, root: Path, seen: _Seen) -> bool:
        """Whether this is the oldest open owner-asked rollover in its checkout. `request.json`
        is one file per checkout, and each owner-asked rollover holds it from its write until
        it ends, so a later one waits rather than overwriting it."""
        for other in await self._store.open_rollovers():
            if other.reason == "owner" and seen.roots.get(other.project_id) == root:
                return other.id == rollover.id
        return False

    async def _start_successor(self, rollover: Rollover, seen: _Seen) -> None:
        if rollover.successor_session_id is not None:
            # Recorded only once SUCCESSOR_STARTING is persisted, so one here is a contradiction;
            # giving up is the reading that cannot launch twice.
            await self._fail(rollover, seen, SUCCESSOR_UNKNOWN)
            return
        if not seen.running(rollover.predecessor_session_id):
            await self._fail(rollover, seen, PREDECESSOR_GONE)
            return
        if not await self._looks_idle(rollover.predecessor_session_id):
            if self._now() - rollover.updated_at >= NOT_IDLE_PATIENCE:
                await self._fail(rollover, seen, PREDECESSOR_NOT_IDLE)
            return
        assert rollover.handoff_id is not None  # HANDOFF_READY is opened from a `ready` only
        starting = await self._store.advance(rollover.id, _S.SUCCESSOR_STARTING, at=self._now())
        try:
            outcome = await self._launch(
                LaunchCommand(
                    rollover.project_id,
                    rollover.profile_id,
                    f"rollover:{rollover.handoff_id}",
                )
            )
        except DuplicateCommandError:
            await self._fail(
                starting, seen, SUCCESSOR_UNKNOWN, "the launch key was already claimed"
            )
            return
        except Exception as error:
            # The launch may have got part-way; its key is claimed, so it is never tried again.
            _LOG.exception("launching the successor for rollover %s failed", rollover.id)
            await self._fail(starting, seen, LAUNCH_FAILED, type(error).__name__)
            return
        record = getattr(outcome, "record", None)
        if record is None:
            await self._fail(starting, seen, LAUNCH_FAILED, "the launch answered with no session")
            return
        starting = await self._store.record_successor(
            rollover.id, record.session_id, at=self._now()
        )
        if record.state is SessionState.UNTRUSTED:
            # Up on its folder-trust dialog: typing would answer the dialog (DEC-099).
            await self._fail(starting, seen, SUCCESSOR_UNTRUSTED)
            return
        if record.state not in _UP:
            await self._fail(starting, seen, SUCCESSOR_FAILED, record.state.value)
            return
        reason = await self._type_template(record.session_id, rollover.handoff_id)
        if reason is not None:
            await self._fail(starting, seen, NOT_TYPED, reason)
            return
        await self._store.advance(rollover.id, _S.ADOPTING, at=self._now())

    async def _type_template(self, session_id: SessionId, handoff_id: str) -> str | None:
        """None once the template landed, else why it was not typed. Never typed twice: a send
        that may have typed is given up (DEC-099), as the schedules' first prompt is."""
        text = TEMPLATE.format(handoff_id=handoff_id)
        started = self._now()
        while True:
            try:
                delivery = await self._send(session_id, text)
            except Exception:
                _LOG.exception("typing the adoption template into %s failed partway", session_id)
                return DeliveryVerdict.UNCONFIRMED.value
            verdict = delivery_verdict(delivery)
            if verdict is DeliveryVerdict.SENT:
                return None
            # Worded as the schedules' first prompt words it, so one reason reads the same
            # whichever pass typed.
            if verdict not in _BOOTING:
                if verdict is DeliveryVerdict.REFUSED and delivery.reason is not None:
                    return delivery.reason.value
                if delivery.reason is PromptReason.MENU:
                    # A `/` command whose menu cannot be read: no dialog is up, so say which.
                    return PromptReason.MENU.value
                return verdict.value
            if self._now() - started >= STARTUP_PATIENCE:
                if verdict in (DeliveryVerdict.NOT_RUNNING, DeliveryVerdict.UNRECOGNISED):
                    return verdict.value
                return "not_ready"
            await self._sleep(RETRY_SECONDS)

    async def _found_starting(self, rollover: Rollover, seen: _Seen) -> None:
        """A launch this pass did not make just now: a restart, or a pass cut short.

        No successor recorded means a launch may or may not have happened, and there is no
        lookup from the key to a session, so it is given up -- never launched again. A
        recorded, running successor may or may not have been typed to; it is never typed to
        again, only waited on, and the adoption's own patience bounds the wait.
        """
        if rollover.successor_session_id is None:
            await self._fail(rollover, seen, SUCCESSOR_UNKNOWN)
        elif seen.up(rollover.successor_session_id):
            await self._store.advance(
                rollover.id,
                _S.ADOPTING,
                at=self._now(),
                detail="found starting; the template is never typed twice",
            )
        else:
            await self._fail(rollover, seen, SUCCESSOR_FAILED)

    async def _await_adoption(self, rollover: Rollover, seen: _Seen) -> None:
        envelope = self._adoption(rollover, seen)
        if envelope is not None and envelope.event is HandoffEvent.ACCEPTED:
            accepted = await self._store.advance(rollover.id, _S.SUCCESSOR_ACCEPTED, at=self._now())
            await self._stop_predecessor(accepted, seen)
        elif envelope is not None:
            await self._fail(rollover, seen, envelope.failure_code or SUCCESSOR_FAILED)
        elif not seen.up(rollover.successor_session_id):
            await self._fail(rollover, seen, SUCCESSOR_FAILED)
        elif self._now() - rollover.updated_at >= ADOPTION_PATIENCE:
            await self._fail(rollover, seen, ADOPTION_TIMEOUT)

    async def _stop_predecessor(self, rollover: Rollover, seen: _Seen) -> None:
        predecessor = seen.sessions.get(rollover.predecessor_session_id)
        if not seen.up(rollover.successor_session_id):
            # Accepted, then gone: stopping the predecessor now would leave nobody holding the
            # work, so it is kept, and the rollover ends failed rather than half-done.
            await self._fail(
                rollover, seen, SUCCESSOR_FAILED, "the successor went away after accepting"
            )
            return
        if predecessor is None or predecessor.state in _STOPPED:
            stopping = await self._store.advance(
                rollover.id, _S.PREDECESSOR_STOPPING, at=self._now()
            )
            await self._complete(stopping, seen, "the predecessor had already stopped")
            return
        # Anything but a running, idle pane waits, and not forever: an ORPHANED or still-starting
        # predecessor is handed to the owner as a failed stop rather than left holding the row.
        if predecessor.state is not SessionState.RUNNING or not await self._looks_idle(
            rollover.predecessor_session_id
        ):
            if self._now() - rollover.updated_at >= NOT_IDLE_PATIENCE:
                stopping = await self._store.advance(
                    rollover.id, _S.PREDECESSOR_STOPPING, at=self._now()
                )
                await self._stop_failed(stopping, seen, PREDECESSOR_NOT_IDLE)
            return
        stopping = await self._store.advance(rollover.id, _S.PREDECESSOR_STOPPING, at=self._now())
        if may_stop_predecessor(stopping):
            # The one stop in this module, and the only branch that reaches it.
            try:
                # Only onto an idle composer, judged again under the stop's own key lock: a turn
                # that started since the look above is never interrupted (DEC-115).
                observation = await self._graceful_stop(
                    GracefulStopCommand(
                        rollover.predecessor_session_id, predecessor.profile_id, only_if_idle=True
                    )
                )
            except Exception as error:
                _LOG.exception("stopping the predecessor of rollover %s failed", rollover.id)
                await self._stop_failed(stopping, seen, STOP_UNCONFIRMED, type(error).__name__)
                return
            if observation.preserved:
                await self._complete(stopping, seen)
            else:
                code = (
                    PREDECESSOR_NOT_IDLE
                    if observation.detail == NOT_IDLE
                    else observation.detail or STOP_UNCONFIRMED
                )
                await self._stop_failed(stopping, seen, code)

    async def _reconcile_stop(self, rollover: Rollover, seen: _Seen) -> None:
        """A stop found in flight. It is never sent again (DEC-004): a stopped predecessor
        completes the rollover, a running one is the owner's, as any failed stop is. One in
        between (a stop still being recorded) is waited on, then handed over the same way."""
        predecessor = seen.sessions.get(rollover.predecessor_session_id)
        if predecessor is None or predecessor.state in _STOPPED:
            await self._complete(rollover, seen)
        elif (
            predecessor.state is SessionState.RUNNING
            or self._now() - rollover.updated_at >= NOT_IDLE_PATIENCE
        ):
            await self._stop_failed(rollover, seen, STOP_UNCONFIRMED)

    # -- endings -----------------------------------------------------------------------------

    async def _fail(
        self, rollover: Rollover, seen: _Seen, code: str, detail: str | None = None
    ) -> None:
        ended = await self._store.advance(
            rollover.id, _S.FAILED, at=self._now(), failure_code=code, failure_detail=detail
        )
        await self._finish(ended, seen)

    async def _stop_failed(
        self, rollover: Rollover, seen: _Seen, code: str, detail: str | None = None
    ) -> None:
        ended = await self._store.advance(
            rollover.id, _S.STOP_FAILED, at=self._now(), failure_code=code, failure_detail=detail
        )
        await self._finish(ended, seen)

    async def _complete(self, rollover: Rollover, seen: _Seen, detail: str | None = None) -> None:
        ended = await self._store.advance(rollover.id, _S.COMPLETED, at=self._now(), detail=detail)
        await self._finish(ended, seen)

    async def _finish(self, rollover: Rollover, seen: _Seen) -> None:
        """A terminal rollover leaves nothing behind: its envelopes, and its predecessor's
        request -- unless another rollover of that predecessor is open and the request is its."""
        root = seen.roots.get(rollover.project_id)
        if root is None:
            return
        if rollover.handoff_id is not None:
            self._envelopes.discard(root, rollover.handoff_id)
        if any(
            other.predecessor_session_id == rollover.predecessor_session_id
            for other in await self._store.open_rollovers()
        ):
            return
        self._envelopes.clear_request(root, str(rollover.predecessor_session_id))

    async def _withdraw_stale_requests(self, seen: _Seen) -> None:
        """A request stands only while its session's rollover is open. It is withdrawn for every
        other running rollable session (only a running one can read it), so an end this pass
        did not see -- the owner's cancel from the other surface, a crash between a terminal
        move and its clean-up -- leaves none behind."""
        open_for = {r.predecessor_session_id for r in await self._store.open_rollovers()}
        for record in seen.sessions.values():
            root = seen.roots.get(record.project_id)
            if (
                root is not None
                and record.profile_id in self._rollable
                and record.state is SessionState.RUNNING
                and record.session_id not in open_for
            ):
                self._envelopes.clear_request(root, str(record.session_id))


def _grouped(
    envelopes: Sequence[HandoffEnvelope],
) -> dict[str, dict[HandoffEvent, HandoffEnvelope]]:
    """Envelopes by handoff id, then by event. Never by order: their timestamps come from the
    files and have one-second resolution, so causality is read off the id and event alone."""
    grouped: dict[str, dict[HandoffEvent, HandoffEnvelope]] = {}
    for envelope in envelopes:
        grouped.setdefault(envelope.handoff_id, {})[envelope.event] = envelope
    return grouped


def _session_id(value: str) -> SessionId | None:
    try:
        return SessionId.parse(value)
    except ValueError:
        return None
