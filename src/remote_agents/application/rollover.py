"""Turn a workflow's `ready` envelope into one fresh successor, then stop its predecessor.

The service runs `RolloverPass.run_once` on a clock of its own, in `serve` only (DEC-115). With
the `rollover.auto_rollover` switch on, each pass walks every open rollover one step further.
Only sessions of a `rollable` profile -- one whose plan executor writes envelopes -- take part:

- a `ready` envelope naming a RUNNING rollable session opens a rollover at HANDOFF_READY -- if
  it is first seen within `READY_FRESHNESS` of being written; an older one is cleared;
- HANDOFF_READY waits for the predecessor's pane to be IDLE, persists SUCCESSOR_STARTING, and
  launches the predecessor's own project and profile under the key `rollover:<handoff_id>`
  (DEC-114), recording the successor the moment the launch returns;
- a RUNNING successor is typed its own provider's adopt template through the guarded send, with the
  schedules' retry policy (DEC-099), and moves to ADOPTING once the send is confirmed (on the
  trust path below, ADOPTING is persisted first);
- a successor that comes up on its folder-trust dialog is held, untyped, in SUCCESSOR_STARTING
  until the owner trusts it, within `TRUST_PATIENCE`, and its predecessor is still running and
  idle, within `NOT_IDLE_PATIENCE` of the trust (DEC-125): then ADOPTING is persisted first and
  the template typed once. The owner is asked by the trust notice every UNTRUSTED session
  already gets;
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
- `FAILED: successor-untrusted` -- the successor was not trusted within `TRUST_PATIENCE`
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
- `FAILED: request-unanswered` -- no `ready` answered the owner's request within
  `REQUEST_PATIENCE` of asking (DEC-119)
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
a time: the oldest open owner-asked rollover holds it until it ends, and later ones wait. A
request waits at most `REQUEST_PATIENCE`, so a plan that never reaches another gate does not hold
the checkout for ever.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from remote_agents.application.commands import (
    UNATTENDED_LAUNCH_WAIT_SECONDS,
    GracefulStopCommand,
    LaunchCommand,
)
from remote_agents.application.errors import DuplicateCommandError, MutationsDraining
from remote_agents.application.notification_policy import rollover_told
from remote_agents.application.prompt_delivery import DeliveryVerdict, delivery_verdict
from remote_agents.application.schedules import RETRY_SECONDS, STARTUP_RETRIES
from remote_agents.application.session_actions import notifiable
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
from remote_agents.ports.rollover_store import (
    TRUST_HOLD,
    TRUSTED_WAITING,
    IllegalRolloverMove,
    Rollover,
    RolloverStore,
)
from remote_agents.ports.terminal import (
    NOT_IDLE,
    PromptDelivery,
    PromptReason,
    TerminalObservation,
)

_LOG = logging.getLogger(__name__)

_S = RolloverState

NOT_IDLE_PATIENCE = timedelta(minutes=10)
"""How long a predecessor may stay not-IDLE at a step that needs it idle before it is given up."""

ADOPTION_PATIENCE = timedelta(minutes=30)
"""How long a typed successor has to write `accepted`, counted from entering ADOPTING."""

TRUST_PATIENCE = timedelta(hours=2)
"""How long a successor may wait on its folder-trust dialog, counted from the hold (DEC-125).
Long enough for a phone left in another room; meanwhile both sessions only sit idle."""

TRUSTED_TYPING = "trusted; typing once"
"""The detail on the ADOPTING move persisted just before typing into a trusted successor."""

REQUEST_PATIENCE = timedelta(hours=24)
"""How long an owner's request waits for the workflow's `ready`, counted from the asking (DEC-119).

A request is answered at the plan's next stage gate, which can be hours away, so the wait is long.
It is bounded because a plan that closes out, or is abandoned, never answers: unbounded, the row
would hold the checkout's one `request.json` for ever. Quiet cancel at the last gate would need a
"plan finished" signal this service cannot see; until the workflow writes one, this is the bound.
"""

READY_FRESHNESS = timedelta(minutes=5)
"""How old a `ready` may be when first seen and still be acted on. The pass looks every 30 s,
so a `ready` first seen older than this was written while nobody was watching -- the switch
off, or `serve` down -- and the plugin then told the owner to resume by hand. Acting on it
later could put a second session on a plan the owner already resumed (DEC-115: only when
switched on), so it is cleared instead, which is the manual path it already fell to."""

PREDECESSOR_NOT_IDLE = "predecessor-not-idle"
PREDECESSOR_GONE = "predecessor-gone"
LAUNCH_FAILED = "launch-failed"
SUCCESSOR_UNTRUSTED = "successor-untrusted"
NOT_TYPED = "not-typed"
ADOPTION_TIMEOUT = "adoption-timeout"
STOP_UNCONFIRMED = "stop-unconfirmed"
NOT_ROLLABLE = "not-rollable"
NO_TEMPLATE = "no adopt template declared"
"""Why a successor was not typed when its profile's vertical stopped declaring a template
while the rollover was open (a revert, across a restart): nothing is typed that it did not
declare. A `not-typed` detail, so worded, never a code (the notifier's code sweep)."""
NO_HANDOFF_ROOT = "no-handoff-root"
REQUEST_UNWRITTEN = "request-unwritten"
REQUEST_LOST = "request-lost"
REQUEST_UNANSWERED = "request-unanswered"

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
class RolloverReport:
    """An ended rollover the owner is told about, with its predecessor as last seen."""

    rollover: Rollover
    predecessor: SessionRecord | None
    successor: SessionRecord | None = None
    """The successor as last seen, when one was launched -- a FAILED rollover leaves it running."""


@dataclass(frozen=True, slots=True)
class _Seen:
    """One pass's view: the sessions, and where each rollable session's envelopes live.

    Keyed by session, not project (BL-116): a session in its own worktree writes its envelopes
    under that worktree's top level, so two sessions of one project can have two roots. A
    session whose writer reported another root works there, and its envelopes are read there
    (DEC-131). A session with no root here -- no checkout and no report, or a worktree that is
    gone and no report -- is never rolled over, and never read under the project's main
    checkout instead.

    `defaults` is the root each session would have had with no report: its worktree's or its
    project's, where it has one."""

    sessions: dict[SessionId, SessionRecord]
    roots: dict[SessionId, Path]
    defaults: dict[SessionId, Path]

    def running(self, session_id: SessionId | None) -> bool:
        record = None if session_id is None else self.sessions.get(session_id)
        return record is not None and record.state is SessionState.RUNNING

    def root_of(self, rollover: Rollover) -> Path | None:
        """The checkout a rollover's envelopes live in: its predecessor's, which its successor
        shares by reusing the folder. Read off the successor once the predecessor is no longer
        listed, so a successor's `accepted` is still found and the envelopes still cleaned."""
        root = self.roots.get(rollover.predecessor_session_id)
        if root is None and rollover.successor_session_id is not None:
            root = self.roots.get(rollover.successor_session_id)
        return root

    def untrusted(self, session_id: SessionId | None) -> bool:
        """Whether a successor is up on its folder-trust dialog, waiting on the owner."""
        record = None if session_id is None else self.sessions.get(session_id)
        return record is not None and record.state is SessionState.UNTRUSTED

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
        adopt_templates: Mapping[ProfileId, str],
        sessions: Callable[[], Awaitable[Sequence[SessionRecord]]],
        handoff_root: Callable[[ProjectId], Path | None],
        worktree_roots: Callable[[], Awaitable[Mapping[SessionId, Path | None]]] | None = None,
        reported_roots: Callable[[], Awaitable[Mapping[SessionId, Path]]] | None = None,
        enabled: Callable[[], Awaitable[bool]],
        idle: Callable[[SessionId], Awaitable[bool]],
        launch: Callable[[LaunchCommand], Awaitable[object]],
        send: Callable[[SessionId, str], Awaitable[PromptDelivery]],
        graceful_stop: Callable[[GracefulStopCommand], Awaitable[TerminalObservation]],
        notify: Callable[[RolloverReport], Awaitable[None]] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        operation: Callable[[], AbstractAsyncContextManager[object]] = nullcontext,
    ) -> None:
        self._store = store
        self._envelopes = envelopes
        #: The only text a rollover types: the successor's own provider's template, declared
        #: in its vertical (DEC-122). An envelope supplies the id, which the reader has already
        #: held to `^h-[0-9a-f]{20}$`; it never supplies text. Its keys are the rollable set.
        self._templates = dict(adopt_templates)
        self._rollable = frozenset(self._templates)
        self._sessions = sessions
        self._handoff_root = handoff_root
        #: The root of every session bound to a worktree, read once per pass (BL-126): None
        #: for one whose worktree is gone. A session absent from it is regular and uses its
        #: project's root.
        self._worktree_roots = worktree_roots
        #: The root each session's handoff writer last reported, read once per pass (DEC-131).
        #: Only roots the one acceptance rule passes reach here; a session absent from it keeps
        #: its default root.
        self._reported_roots = reported_roots
        self._enabled = enabled
        self._idle = idle
        self._launch = launch
        self._send = send
        self._graceful_stop = graceful_stop
        self._notify = notify
        self._now = now
        self._sleep = sleep
        #: The session service's shutdown gate (`SessionLocks.operation`), held from persisting
        #: a launch or stop to finishing it. Shut, it refuses before anything is persisted, so
        #: the rollover waits for the next start instead of failing; open, the drain waits for
        #: the act to finish (BL-130). Persisted states never move back, so this is the only
        #: order in which a shutdown cannot spend a rollover.
        self._operation = operation
        self._recovered = False

    @property
    def adopt_templates(self) -> Mapping[ProfileId, str]:
        """What this pass types per profile; its keys are the profiles it rolls over."""
        return dict(self._templates)

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
            for root in dict.fromkeys(seen.roots.values()):
                try:
                    await self._pick_up_ready(root, seen)
                except Exception:
                    # One checkout's envelopes cost that checkout this pass, never the steps of
                    # rollovers already open -- an accepted one's stop among them.
                    _LOG.exception(
                        "handoff envelopes in %s could not be read; tried next pass", root
                    )
            for rollover in await self._store.open_rollovers():
                try:
                    await self._step(rollover, seen)
                except IllegalRolloverMove:
                    # Another writer moved it since it was read (the owner's cancel); its move
                    # stands, and the next pass reads where it now is.
                    _LOG.info("rollover %s moved under this pass; left as it is", rollover.id)
                except MutationsDraining:
                    # The service is stopping and the gate is shut. Nothing was persisted for
                    # this step, and every later rollover would meet the same gate.
                    _LOG.debug("the rollover pass met the shutdown drain; resumed next start")
                    break
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
                trust_held=rollover.state is _S.SUCCESSOR_STARTING
                and (
                    seen.untrusted(rollover.successor_session_id)
                    or await self._trust_hold(rollover) is not None
                ),
                successor_on_trust_dialog=seen.untrusted(rollover.successor_session_id),
            )
            action = recovery_action(rollover.state, facts)
            await self._store.note(rollover.id, f"restart: {action.value}", at=self._now())

    async def _look(self) -> _Seen:
        sessions = {record.session_id: record for record in await self._sessions()}
        bound = {} if self._worktree_roots is None else await self._worktree_roots()
        reported = {} if self._reported_roots is None else await self._reported_roots()
        project_roots: dict[ProjectId, Path | None] = {}
        roots: dict[SessionId, Path] = {}
        defaults: dict[SessionId, Path] = {}
        for record in sessions.values():
            if record.profile_id not in self._rollable:
                continue
            if record.session_id in bound:
                root = bound[record.session_id]
            else:
                if record.project_id not in project_roots:
                    project_roots[record.project_id] = self._handoff_root(record.project_id)
                root = project_roots[record.project_id]
            if root is not None:
                defaults[record.session_id] = root
            # The writer's own report, when it made one, is where it works (DEC-131) -- also for
            # a session whose worktree is gone: it has no default, and the report is a root of
            # its own. Bounded as any report is: a `ready` there answers only for this session
            # (DEC-115), and only a git top level this user owns is ever reported.
            root = reported.get(record.session_id, root)
            if root is not None:
                roots[record.session_id] = root
        return _Seen(sessions, roots, defaults)

    # -- envelopes ---------------------------------------------------------------------------

    async def _pick_up_ready(self, root: Path, seen: _Seen) -> None:
        """Open, or find again, the rollover each `ready` in this checkout names -- for a
        session whose own root this is, and no other."""
        held = {r.handoff_id for r in await self._store.open_rollovers() if r.handoff_id}
        for handoff_id, group in _grouped(self._envelopes.events(root)).items():
            ready = group.get(HandoffEvent.READY)
            if ready is None:
                # An `accepted` or `failed` with no `ready` belongs to no rollover this
                # service can still act on -- one already finished, or a forgery.
                if handoff_id not in held:
                    self._envelopes.discard(root, handoff_id)
                continue
            if handoff_id not in held and self._now() - ready.timestamp > READY_FRESHNESS:
                # Nobody was watching when it was written (see `READY_FRESHNESS`).
                self._envelopes.discard(root, handoff_id)
                continue
            predecessor = _session_id(ready.managed_session_id)
            record = None if predecessor is None else seen.sessions.get(predecessor)
            # A `ready` answers only under its session's one root -- reported, worktree or project
            # -- so a file in a root another session reported never speaks for this one (DEC-115).
            if (
                record is None
                or seen.roots.get(record.session_id) != root
                or record.profile_id not in self._rollable
                or record.state is not SessionState.RUNNING
            ):
                continue
            rollover = await self._store.open_for_ready(
                record.session_id,
                handoff_id,
                project_id=record.project_id,
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
        root = seen.root_of(rollover)
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
        root = seen.root_of(rollover)
        if root is None:
            await self._fail(rollover, seen, NO_HANDOFF_ROOT)
            return
        if self._now() - rollover.requested_at > REQUEST_PATIENCE:
            # Given up, which frees the checkout's slot; `_finish` withdraws the request.
            await self._fail(rollover, seen, REQUEST_UNANSWERED)
            return
        holder = await self._request_slot_holder(root, seen)
        if holder is not None and holder.id != rollover.id:
            await self._note_waiting(rollover, holder)
            return
        wanted = str(rollover.predecessor_session_id)
        if await self._store.record_request(rollover.id, at=self._now()):
            if not self._envelopes.write_request(root, wanted):
                await self._fail(rollover, seen, REQUEST_UNWRITTEN)
        elif self._envelopes.requested(root) != wanted:
            # Recorded as written, and not there: a crash between the record and the write, or
            # the file removed or replaced by someone else. Never written twice (DEC-004).
            await self._fail(rollover, seen, REQUEST_LOST)

    async def _request_slot_holder(self, root: Path, seen: _Seen) -> Rollover | None:
        """The oldest open owner-asked rollover in a checkout. `request.json` is one file per
        checkout, and each owner-asked rollover holds it from its write until it ends, so a
        later one waits rather than overwriting it."""
        for other in await self._store.open_rollovers():
            if other.reason == "owner" and seen.root_of(other) == root:
                return other
        return None

    async def _note_waiting(self, rollover: Rollover, holder: Rollover) -> None:
        """Say once, on the waiting row, whose request it waits behind -- a wait nothing else
        would show until `REQUEST_PATIENCE`, 24 hours from the asking, gives the request up."""
        detail = f"waiting: {holder.predecessor_session_id}'s request holds this checkout"
        if any(event.detail == detail for event in await self._store.events(rollover.id)):
            return
        await self._store.note(rollover.id, detail, at=self._now())

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
        async with self._operation():
            starting = await self._store.advance(rollover.id, _S.SUCCESSOR_STARTING, at=self._now())
            try:
                outcome = await self._launch(
                    LaunchCommand(
                        rollover.project_id,
                        rollover.profile_id,
                        f"rollover:{rollover.handoff_id}",
                        guard_wait=UNATTENDED_LAUNCH_WAIT_SECONDS,
                        # The successor carries on in its predecessor's folder: the same
                        # worktree when it has one (BL-116, amending DEC-115).
                        reuse_folder_of=rollover.predecessor_session_id,
                    )
                )
            except DuplicateCommandError:
                await self._fail(
                    starting, seen, SUCCESSOR_UNKNOWN, "the launch key was already claimed"
                )
                return
            except Exception as error:
                # The launch may have got part-way; its key may be claimed, so it is never tried
                # again. A `CodexRemoteControlBusy` lands here too, after the unattended wait:
                # nothing started, but SUCCESSOR_STARTING is persisted and cannot step back, so
                # the rollover fails under that name (BL-037's residual for a wedged change).
                _LOG.exception("launching the successor for rollover %s failed", rollover.id)
                await self._fail(starting, seen, LAUNCH_FAILED, type(error).__name__)
                return
            record = getattr(outcome, "record", None)
            if record is None:
                await self._fail(
                    starting, seen, LAUNCH_FAILED, "the launch answered with no session"
                )
                return
            # Inside the hold too: a successor launched but not recorded is the one state a
            # restart can only give up on (successor-unknown). One up on its folder-trust
            # dialog is recorded with its hold, in one transaction: typing would answer the
            # dialog (DEC-099), so it is held until the owner trusts it, and no restart can
            # find it recorded but not held (DEC-125).
            held = record.state is SessionState.UNTRUSTED
            starting = await self._store.record_successor(
                rollover.id, record.session_id, at=self._now(), held=held
            )
        if held:
            # `_found_starting` takes it from here.
            return
        if record.state not in _UP:
            await self._fail(starting, seen, SUCCESSOR_FAILED, record.state.value)
            return
        reason = await self._type_template(
            record.session_id, rollover.profile_id, rollover.handoff_id
        )
        if reason is not None:
            await self._fail(starting, seen, NOT_TYPED, reason)
            return
        await self._store.advance(rollover.id, _S.ADOPTING, at=self._now())

    async def _type_template(
        self, session_id: SessionId, profile_id: ProfileId, handoff_id: str
    ) -> str | None:
        """None once the template landed, else why it was not typed. Never typed twice: a send
        that may have typed is given up (DEC-099), as the schedules' first prompt is."""
        template = self._templates.get(profile_id)
        if template is None:
            return NO_TEMPLATE
        text = template.format(handoff_id=handoff_id)
        retries = 0  # counted, never read off the clock (`STARTUP_RETRIES`)
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
            if retries >= STARTUP_RETRIES:
                if verdict in (DeliveryVerdict.NOT_RUNNING, DeliveryVerdict.UNRECOGNISED):
                    return verdict.value
                return "not_ready"
            retries += 1
            await self._sleep(RETRY_SECONDS)

    async def _found_starting(self, rollover: Rollover, seen: _Seen) -> None:
        """A launch this pass did not make just now: a restart, a pass cut short, or a successor
        held on its folder-trust dialog.

        No successor recorded means a launch may or may not have happened, and there is no
        lookup from the key to a session, so it is given up -- never launched again. A
        recorded, running successor may or may not have been typed to; it is never typed to
        again, only waited on, and the adoption's own patience bounds the wait.

        A successor held on its trust dialog (`TRUST_HOLD`) was never typed to: nothing is
        typed while it is up. It is waited on for `TRUST_PATIENCE`. Once the owner trusts it,
        its predecessor must be running and idle; a working one is waited on for
        `NOT_IDLE_PATIENCE`, counted from the first pass that found it so (`TRUSTED_WAITING`).
        Then ADOPTING is persisted *before* the template is typed, so a restart part-way finds
        ADOPTING, which waits and never types (DEC-099: never typed twice).
        """
        if rollover.successor_session_id is None:
            await self._fail(rollover, seen, SUCCESSOR_UNKNOWN)
            return
        details: dict[str | None, datetime] = {}
        for event in await self._store.events(rollover.id):
            details.setdefault(event.detail, event.created_at)  # each one's first time
        held_at = details.get(TRUST_HOLD)
        if held_at is None and seen.untrusted(rollover.successor_session_id):
            # On its dialog with no hold recorded: a row a 0.63.0 pass left, which wrote the
            # hold after the record and could end between the two. Nothing was typed: only a
            # launch settle records UNTRUSTED (DEC-081), and the launch types only into a
            # successor up on its composer. So it is held from now.
            await self._store.note(rollover.id, TRUST_HOLD, at=self._now())
            return
        if held_at is None:
            if seen.up(rollover.successor_session_id):
                await self._store.advance(
                    rollover.id,
                    _S.ADOPTING,
                    at=self._now(),
                    detail="found starting; the template is never typed twice",
                )
            else:
                await self._fail(rollover, seen, SUCCESSOR_FAILED)
            return
        if seen.untrusted(rollover.successor_session_id):
            if self._now() - held_at >= TRUST_PATIENCE:
                await self._fail(rollover, seen, SUCCESSOR_UNTRUSTED)
            return
        if not seen.up(rollover.successor_session_id):
            # Declined ("No, exit"), stopped, or failed while it was asking.
            await self._fail(rollover, seen, SUCCESSOR_FAILED)
            return
        # The hold can last hours, and the owner may have gone back to the old session: its
        # handoff is then stale, and two sessions would work one plan. So the predecessor must
        # still be running and idle -- the launch's own condition -- before the template goes in.
        if not seen.running(rollover.predecessor_session_id):
            await self._fail(rollover, seen, PREDECESSOR_GONE)
            return
        if not await self._looks_idle(rollover.predecessor_session_id):
            # Its own clock, started now: the trust may have come late in its patience, and a
            # session busy at that moment is owed the launch's wait for idle, not none.
            waiting_since = details.get(TRUSTED_WAITING)
            if waiting_since is None:
                await self._store.note(rollover.id, TRUSTED_WAITING, at=self._now())
            elif self._now() - waiting_since >= NOT_IDLE_PATIENCE:
                await self._fail(rollover, seen, PREDECESSOR_NOT_IDLE)
            return
        adopting = await self._store.advance(
            rollover.id, _S.ADOPTING, at=self._now(), detail=TRUSTED_TYPING
        )
        assert adopting.handoff_id is not None  # opened from a `ready`, which names one
        reason = await self._type_template(
            rollover.successor_session_id, rollover.profile_id, adopting.handoff_id
        )
        if reason is not None:
            await self._fail(adopting, seen, NOT_TYPED, reason)

    async def _trust_hold(self, rollover: Rollover) -> datetime | None:
        """When this rollover's successor was held on its trust dialog, or None if it never was."""
        for event in await self._store.events(rollover.id):
            if event.detail == TRUST_HOLD:
                return event.created_at
        return None

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
        async with self._operation():
            stopping = await self._store.advance(
                rollover.id, _S.PREDECESSOR_STOPPING, at=self._now()
            )
            if may_stop_predecessor(stopping):
                # The one stop in this module, and the only branch that reaches it.
                try:
                    # Only onto an idle composer, judged again under the stop's own key lock: a turn
                    # that started since the look above is never interrupted (DEC-115).
                    observation = await self._graceful_stop(
                        GracefulStopCommand(
                            rollover.predecessor_session_id,
                            predecessor.profile_id,
                            only_if_idle=True,
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
        # Told before the clean-up, which touches the filesystem and may raise: the move is
        # made once, so a notice skipped here would never be made again.
        await self._tell(ended, seen)
        await self._finish(ended, seen)

    async def _stop_failed(
        self, rollover: Rollover, seen: _Seen, code: str, detail: str | None = None
    ) -> None:
        ended = await self._store.advance(
            rollover.id, _S.STOP_FAILED, at=self._now(), failure_code=code, failure_detail=detail
        )
        # Told before the clean-up, which touches the filesystem and may raise: the move is
        # made once, so a notice skipped here would never be made again.
        await self._tell(ended, seen)
        await self._finish(ended, seen)

    async def _complete(self, rollover: Rollover, seen: _Seen, detail: str | None = None) -> None:
        ended = await self._store.advance(rollover.id, _S.COMPLETED, at=self._now(), detail=detail)
        await self._finish(ended, seen)

    async def _tell(self, rollover: Rollover, seen: _Seen) -> None:
        """Hand an ended rollover to the notifier, once: called at the move itself, which the
        store makes only once. Never raises -- a notice lost is logged, never a pass lost."""
        if self._notify is None or not rollover_told(rollover.state):
            return
        try:
            successor = rollover.successor_session_id
            await self._notify(
                RolloverReport(
                    rollover,
                    seen.sessions.get(rollover.predecessor_session_id),
                    None if successor is None else seen.sessions.get(successor),
                )
            )
        except Exception:
            _LOG.exception("the notice for rollover %s could not be handed on", rollover.id)

    async def _finish(self, rollover: Rollover, seen: _Seen) -> None:
        """A terminal rollover leaves nothing behind: its envelopes, and its predecessor's
        request -- unless another rollover of that predecessor is open and the request is its."""
        root = seen.root_of(rollover)
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
            root = seen.roots.get(record.session_id)
            if (
                root is not None
                and record.profile_id in self._rollable
                and record.state is SessionState.RUNNING
                and record.session_id not in open_for
            ):
                self._envelopes.clear_request(root, str(record.session_id))


def rollover_notice_settled(
    rollovers: RolloverStore,
    session: Callable[[SessionId], Awaitable[SessionRecord | None]],
) -> Callable[[str], Awaitable[bool]]:
    """Whether a failed rollover's message is over before its lifetime, by rollover id.

    Over when the rollover is gone, when a newer rollover of the same session has opened, or
    when the session the failure left running has stopped -- the owner has acted on it. A
    `predecessor-gone` failure never left it running, so only time or a newer rollover ends
    that one; otherwise its message would go before the owner could read it.
    """

    async def settled(rollover_id: str) -> bool:
        rollover = await rollovers.get(rollover_id)
        if rollover is None:
            return True
        latest = await rollovers.latest_for(rollover.predecessor_session_id)
        if latest is not None and latest.id != rollover.id:
            return True
        if rollover.failure_code == PREDECESSOR_GONE:
            return False
        record = await session(rollover.predecessor_session_id)
        return record is None or not notifiable(record.state)

    return settled


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
