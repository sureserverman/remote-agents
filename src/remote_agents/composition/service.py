"""Compose the serving loop: the bot boundary plus its record-keeping companions."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    contextmanager,
    nullcontext,
)
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from remote_agents.adapters.git.checkout import GitCli
from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
from remote_agents.adapters.telegram.limit_reset_notifications import LimitResetNotifier
from remote_agents.adapters.telegram.rollover_notifications import RolloverNotifier
from remote_agents.adapters.telegram.schedule_notifications import ScheduleNotifier
from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.adapters.telegram.trust_notifications import TrustNotifier
from remote_agents.adapters.tmux.runtime import TmuxTerminal
from remote_agents.adapters.workflow.handoff_envelopes import FileHandoffEnvelopes
from remote_agents.adapters.workflow.roots import acceptable_root, handoff_root
from remote_agents.application.activity import CodexApprovalWatcher, drain_activity
from remote_agents.application.backend import CLOSE_TIMEOUT_SECONDS, DRAIN_TIMEOUT_SECONDS
from remote_agents.application.commands import ResumeCommand
from remote_agents.application.interrupted_resume import (
    InterruptedResumePass,
    RestartSnapshot,
    ResumeReport,
    SessionAtRestart,
)
from remote_agents.application.limit_lifts import LimitLiftWatcher
from remote_agents.application.limit_resume import prune_outcomes
from remote_agents.application.limit_stops import LimitScreenWatcher, LimitStopClassifier
from remote_agents.application.reconcile import ReconciliationService
from remote_agents.application.resume_flow import resume_capable
from remote_agents.application.rollover import RolloverPass, RolloverReport
from remote_agents.application.schedules import SchedulePass
from remote_agents.application.services import ResumeOutcome, SessionService
from remote_agents.application.session_actions import notifiable
from remote_agents.application.session_folders import live_folder
from remote_agents.application.worktree_cleanup import WorktreeCleanupPass
from remote_agents.application.worktrees import main_checkout
from remote_agents.config import TelegramSecrets
from remote_agents.domain.conversations import ProfileResumeCapability
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind, AgentActivity
from remote_agents.ports.folder_bindings import FolderBindings
from remote_agents.ports.git_checkout import GitCheckout
from remote_agents.ports.interrupted_resumes import RESUMING, InterruptedResumes
from remote_agents.ports.limit_stop_outcomes import LimitStopOutcomes
from remote_agents.ports.message_relay import MessageRelay, RelayOutcome, RelayResult
from remote_agents.ports.reported_roots import ReportedRoots
from remote_agents.ports.session_store import SessionStore
from remote_agents.ports.state_events import StoreChanged
from remote_agents.ports.terminal import PromptDelivery
from remote_agents.ports.turn_markers import ConversationMarks, TurnMarkers
from remote_agents.ports.worktree_store import Worktree, WorktreeStore

_LOG = logging.getLogger(__name__)

#: The activity pass's period when a caller names none. `bootstrap` passes the configured
#: `limits.activity_poll_seconds`, whose own default is this same 30.
_ACTIVITY_POLL_SECONDS = 30.0
#: How often the folder-trust question is looked for. Six times the activity cadence, and the
#: difference is what the two passes are waiting on. An activity report is news about work that
#: has already happened, so half a minute late is half a minute late. A trust question is a
#: session standing still until the owner answers it, and the owner asked for it to appear
#: "immediately" -- neither the 30 s activity poll nor the 60 s reconcile is that. The pass is
#: cheap: it reads the records it already has and touches Telegram only when something changed.
_TRUST_POLL_SECONDS = 5.0
#: How often each provider is asked what its plan's windows now read. Sixty times the trust
#: cadence, and the gap is what the two are waiting on. A trust question is a session standing
#: still until the owner answers it. An early limits reset is not waiting on anybody: nothing
#: the owner does changes it, and the figures are already on the limits screen for whoever
#: looks. What the message buys is *knowing without looking*, and five minutes is prompt for
#: that while costing one bounded read per provider per tick against a rate-limit endpoint.
#:
#: It is deliberately *not* compared to `EARLY_RESET_GRACE` here. An earlier version of this
#: comment called the period "comfortably wider" than that grace while the grace's own comment
#: claimed the reverse, and the two values are both five minutes, so neither statement was
#: true. The two numbers answer unrelated questions: this one is how often the figures are
#: asked for, and the grace is how far ahead of its published instant a wipe must be to count
#: as early. A scheduled rollover is silent whatever either of them is set to.
_LIMITS_POLL_SECONDS = 300.0
#: How often an undecided limit stop is asked whether it lifted. The activity pass's clock: a
#: lift is acted on within half a minute of its reset's grace minute, and a pass with no running
#: session stopped by a limit is a session listing plus one indexed seek per running session.
_LIMIT_STOP_POLL_SECONDS = 30.0

#: How often due schedules are looked for. A schedule fires up to this late, well inside the
#: fifteen minutes a fire may still start (`schedules.MISSED_GRACE`).
_SCHEDULE_POLL_SECONDS = 30.0

#: How often finished limit-stop outcomes older than `OUTCOME_RETENTION` are pruned (BL-111).
#: Daily, because the retention is ninety days; the first pass waits a minute after start so it
#: never competes with the start-up passes.
_PRUNE_POLL_SECONDS = 86400.0
_PRUNE_FIRST_DELAY_SECONDS = 60.0
#: How often envelopes and open rollovers are walked (DEC-115). The schedules' clock: a `ready`
#: is acted on within half a minute of the gate that wrote it, and every patience the pass
#: counts (ten minutes for an idle pane, thirty for an adoption) is many ticks long.
_ROLLOVER_POLL_SECONDS = 30.0
#: How often `serve` looks for a session worktree it may remove (BL-116). Nothing waits on a
#: removal: a merged, ended folder that stays a minute longer costs disk, never work.
_WORKTREE_POLL_SECONDS = 60.0

_RESTART_NOTICE_POLL_SECONDS = 1.0
"""How often the held restart notice is retried: the bot attaches moments after the pass."""

_RESTART_STOP_GRACE_SECONDS = 5.0
"""How long shutdown waits, after the drain, for the restart pass to record its stop."""
#: Bounds one limit-stop pass (a limits read, the session and outcome reads, a Telegram edit or
#: delete per lifted stop), so one wedged call costs a pass rather than every later lift.
_LIMIT_STOP_PASS_TIMEOUT_SECONDS = 20.0

#: Bounds one pass's limit-stop classification (a limits read, one session lookup per stop), so a
#: wedged reader delays this pass's delivery by at most this long; the stops then go window-blind.
_CLASSIFY_TIMEOUT_SECONDS = 12.0
#: How often a waiting message is re-checked while its session's turn may have ended with no
#: "finished" event to say so (BL-108): an Esc fires no hook, and a turn a limit killed fires
#: none on Codex. The owner accepted 2-3 s. A tick takes no capture unless a marked or
#: just-finished session has a message waiting; its only reads are the marker listing and, for
#: each such session, one lookup of its waiting message.
_FAST_CHECK_SECONDS = 2.5
#: How long after a "finished" activity a waiting message whose retry still queued it (busy, or
#: a dialog) is re-checked. Claude draws "(running Stop hooks...)" until every Stop hook has run,
#: and ours is one of them, so a retry on its record can land while that is still on screen:
#: measured 2026-09-24, the screen turned idle 0-60 ms after the record appeared, with the
#: owner's other Stop hooks taking longer on a busier host. Its Stop has already removed the
#: marker, so without this the message would wait for another turn.
_JUST_FINISHED_SECONDS = 15.0

#: How long a turn that paused for its own background work must stay quiet before its "finished"
#: is delivered (DEC-129, owner's choice 2026-10-17). A shell or subagent that reports back wakes
#: the session well inside it; a dev server left running never does, and must not silence the
#: session for good.
_PAUSED_QUIET_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class ServiceComposition:
    """The bot boundary plus what the service needs to keep records honest beside it."""

    boundary: PrivateBotBoundary
    terminal: TmuxTerminal
    reconciler: ReconciliationService
    approval_watcher: CodexApprovalWatcher | None = None
    """None only in compositions that do not wire pane watching, which today means tests.

    Production always supplies one -- `_private_boundary` builds it unconditionally -- and it
    simply has nothing to do on a pass where no Codex session is running. The field is optional
    so that every composition predating it still constructs.

    Renamed on 2026-08-30, when the pane-digest watch it was named for was retired along with
    the `quiet` activity kind. `ServiceComposition` is constructed positionally in places, so
    the field name is what a reader consults to find out what this service observes; keeping the
    old one would have gone on describing a mechanism that had been deleted.
    """

    activity_directory: Path | None = None
    """Where the agent hooks spool what they reported, or None when nothing spools.

    The second of the two activity sources, and the reason the periodic pass runs even for a
    composition with no approval watcher: a host running only Claude sessions has no pane
    anything here can observe -- `HOOK_EXCLUSIVE` and `UNOBSERVED` are both skipped -- and a
    spool full of what those sessions reported.
    """

    activity_store: SQLiteActivityStore | None = None
    """Where every observation becomes durable before delivery, or None to skip recording.

    The local feed's source (migration 9), never a delivery ledger — DEC-026's in-memory
    notifier state is unchanged. Recorded *before* `deliver` so the feed shows what was
    observed even when Telegram refuses the send; a failed append costs the feed one row
    and never costs the phone its notification.
    """

    limit_reset_notifier: LimitResetNotifier | None = None
    """The pass that notices a provider wiping its meters early, or None where nothing does.

    None in compositions that wire no Telegram — the message is bot-only (DEC-097) — and in
    every composition predating it, which is why it defaults. The watch is created only when
    this is wired: the notifier is what holds the baseline, so without one there is nothing for
    a loop to tick.

    Placed after `trust_notifier` for the reason that field's own note gives: this dataclass is
    constructed positionally in places, so a new field goes at the end with a default.
    """

    trust_notifier: TrustNotifier | None = None
    """The pass that asks an untrusted session's question, or None where nothing asks it.

    None in compositions that wire no Telegram -- the local surface never asks (DEC-047) -- and
    in tests predating it. Typed loosely for the reason the fields above are ordered as they
    are: `ServiceComposition` is constructed positionally in places, so a new field goes at the
    end with a default and names what it is in prose.
    """

    prompt_relay: MessageRelay | None = None
    """The relay whose waiting messages a "finished" activity retries (DEC-099), or None.

    None where no relay is wired -- every composition but the bot's -- which is why it defaults
    and sits at the end.
    """

    relay_announcer: Callable[[str, RelayResult], Awaitable[None]] | None = None
    """What tells the owner a waiting message was delivered (or not), or None to stay silent."""

    turn_markers: TurnMarkers | None = None
    """Which sessions' hooks said a turn started (DEC-104), for the fast check, or None where no
    relay is wired."""

    paused_turns: dict[str, tuple[AgentActivity, float]] = field(
        default_factory=dict, repr=False, compare=False
    )
    """Each session's held "finished" from a turn that paused for background work, with when it
    was drained (monotonic), for `_hold_paused_turns` (DEC-129). In memory only: a restart loses
    a held one, and that session says "finished" at its next real end."""

    relay_rechecks: dict[str, float] = field(default_factory=dict, repr=False, compare=False)
    """Sessions whose retry on a "finished" activity still queued (busy or a dialog), by when
    (monotonic), for the fast check's just-finished window. Mutable state on a frozen
    composition, deliberately: it is shared by the activity pass that fills it and the fast
    check that drains it.

    It protects nothing by itself. A new turn the owner starts inside the window is kept from
    being typed into by the retry's own capture, which reads that turn's fresh marker (its
    `UserPromptSubmit` fired before it started) and refuses as busy."""

    limit_screen_watcher: LimitScreenWatcher | None = None
    """The pass that reads a limit stop off the pane of an agent that reports none, or None.

    None where nothing wires it -- every composition but the bot's -- and then Codex and Cursor
    stops go unobserved, exactly as they did before it existed.
    """

    limit_lift_watcher: LimitLiftWatcher | None = None
    """The pass that notices a limit stop has lifted and retires its line, or None.

    None where nothing wires it -- every composition but the bot's -- and then a stop's line
    stays until later news retires it, exactly as before it existed.
    """

    limit_classifier: LimitStopClassifier | None = None
    """What gives each limit stop the window that stopped it before it is recorded, or None.

    None in compositions that wire no limits read -- every one but the bot's -- where a limit
    stop is recorded and delivered as it arrived, window-blind, exactly as before it existed.
    """

    schedule_pass: SchedulePass | None = None
    """The pass that fires due schedules (DEC-114), or None where nothing fires them.

    The bot's composition only. The local surface manages schedules and never fires one, so a
    schedule can be fired by one process alone.
    """

    schedule_notifier: ScheduleNotifier | None = None
    """Where the pass's notices go, retried each tick while one is held, or None."""

    rollover_pass: RolloverPass | None = None
    """The pass that turns a workflow's handoff into a successor (DEC-115), or None.

    The bot's composition only, as `schedule_pass` is: the local surface asks for a rollover by
    writing a row, and only this process launches, types or stops for one, so the pass's
    in-memory view and the panes it acts on never belong to two processes (DEC-030).
    """

    rollover_notifier: RolloverNotifier | None = None
    """Where the pass's failure notices go, retried each tick while one is held, or None."""

    worktree_cleanup: WorktreeCleanupPass | None = None
    """The pass that removes a merged, ended, unreferenced session worktree (BL-116), or None.

    The bot's composition only, as `rollover_pass` is: the TUI never runs it, so one process
    per host removes folders, and its claims are given back by that process alone.
    """

    limit_stop_outcomes: LimitStopOutcomes | None = None
    """What became of each limit stop, pruned once a day after `OUTCOME_RETENTION`, or None."""

    relay_notice_flush: Callable[[], Awaitable[None]] | None = None
    """Sends the queued-message notices a flood hold kept, on every activity pass (BL-107)."""

    drain: Callable[[], Awaitable[None]] | None = None
    """Closes the mutation gate and waits for operations in flight: the one `SessionLocks`
    the bot's services share (DEC-046), so the drain counts every operation. None in
    compositions that wire no lock set, which today means tests."""

    live_session: Callable[[str], Awaitable[bool]] | None = None
    """Whether an observation's session is one the feed may still record news about (DEC-031).

    The feed's table and the bot answer one question about a session that has ended: neither
    tells the owner something new happened in it. None records every observation, which today
    means tests.
    """

    hook_roster: Callable[[], Awaitable[None]] | None = None
    """Rewrites the hook rosters before each activity drain (DEC-126), so a session that ended
    leaves them. None in compositions with no payload-identified hook, which today means tests."""

    restart_resume: RestartResume | None = None
    """The startup pass that resumes what a lost tmux server interrupted (DEC-127), or None.

    The bot's composition only, as `rollover_pass` is: one process launches unattended."""

    restart_notifier: object | None = None
    """Where the restart notice is held until the bot is attached, flushed beside the periodic
    tasks until it is sent; None where nothing tells the owner."""


@dataclass(frozen=True)
class RestartResume:
    """What `serve` needs to resume, at startup, the sessions a lost tmux server interrupted."""

    snapshot: Callable[[], Awaitable[RestartSnapshot | None]]
    """Read before the first reconcile, while the lost records are still RUNNING. Never raises:
    None when the records themselves could not be read."""
    enabled: Callable[[], Awaitable[bool]]
    """The Settings switch, read toward off (`read_resume_interrupted(when_unsure=False)`)."""
    resume: InterruptedResumePass
    forget_marks: Callable[[], Awaitable[None]]
    """Forget the conversation marks of every ENDED session, so they do not pile up; a live or
    FAILED session, and one an earlier start left `resuming`, keeps its mark for a later resume."""
    notify: Callable[[tuple[ResumeReport, ...], int], Awaitable[None]] | None = None
    """Tell the owner once: the reports, and how many sessions could not be judged."""
    exclusive: Callable[[], AbstractContextManager[bool]] = lambda: nullcontext(True)
    """Held for the whole pass; False when another process holds it, and then this one resumes
    nothing. Two `serve` processes on one store would otherwise each launch, and each overwrite
    the other's outcome."""


def build_restart_resume(
    *,
    store: SessionStore,
    terminal: object,
    turn_markers: TurnMarkers,
    conversation_marks: ConversationMarks,
    rollovers: SQLiteRolloverStore,
    capabilities: Callable[[], Awaitable[tuple[ProfileResumeCapability, ...]]],
    outcomes: InterruptedResumes,
    resume: Callable[[ResumeCommand], Awaitable[ResumeOutcome]],
    folder_of: Callable[[SessionId], Awaitable[Worktree | None]],
    bound_folder_of: Callable[[SessionId], Awaitable[Path | None]] | None = None,
    enabled: Callable[[], Awaitable[bool]],
    notify: Callable[[tuple[ResumeReport, ...], int], Awaitable[None]] | None,
    stopping: Callable[[], bool],
    lock_path: Path | None = None,
    remember_permissions: Callable[[], Awaitable[None]] | None = None,
) -> RestartResume:
    """The restart resume pass over the bot's stores and terminal (DEC-127)."""

    async def snapshot() -> RestartSnapshot | None:
        try:
            records = tuple(await store.list())
        except Exception:
            _LOG.exception("the sessions could not be read for the restart resume pass")
            return None
        known = True
        started: datetime | None = None
        listed: set[str] = set()
        marks: dict[str, str] = {}
        markers: frozenset[str] = frozenset()
        handed: list = []
        resumable: frozenset[ProfileId] = frozenset()
        pending: frozenset[str] = frozenset()
        try:
            # Raises on anything but an absent server: then nothing is resumed on a guess.
            started = await terminal.server_started_at()  # type: ignore[attr-defined]
            listed = {
                str(observation.session_id)
                for observation in await terminal.managed_observations()  # type: ignore[attr-defined]
            }
            # Unreadable is not empty: listed as empty, every session would be skipped for good
            # (`no-conversation`) or resumed without its nudge.
            if not (turn_markers.listable() and conversation_marks.listable()):
                raise RuntimeError("the turn markers or conversation marks cannot be listed")
            markers = frozenset(turn_markers.sessions())
            for session in conversation_marks.sessions():
                mark = conversation_marks.latest(session)
                if mark is not None:
                    marks[session] = mark
            pending = frozenset(
                session
                for session, outcome in (await outcomes.outcomes()).items()
                if outcome == RESUMING
            )
            for record in records:
                # A pending session is ENDED by now, and may have handed off since it was first
                # judged: its rollover is read on the retry too.
                if record.state is SessionState.RUNNING or str(record.session_id) in pending:
                    latest = await rollovers.latest_for(record.session_id)
                    if latest is not None:
                        handed.append(latest)
            resumable = frozenset(
                capability.profile_id
                for capability in await capabilities()
                if resume_capable(capability)
            )
        except Exception:
            _LOG.exception("what a restart interrupted could not be read; nothing is resumed")
            known = False
        return RestartSnapshot(
            sessions=tuple(
                SessionAtRestart(record, str(record.session_id) in listed) for record in records
            ),
            server_started_at=started,
            server_known=known,
            turn_marker_sessions=markers,
            conversation_marks=marks,
            rollovers=tuple(handed),
            resumable_profiles=resumable,
            pending=pending,
        )

    async def forget_marks() -> None:
        if remember_permissions is not None:
            await remember_permissions()
        # Only ENDED, never merely not RUNNING: an UNTRUSTED or STARTING session is live, and
        # FAILED can still be promoted; their marks are what a later resume would need.
        kept = {
            str(record.session_id)
            for record in await store.list()
            if record.state is not SessionState.ENDED
        }
        pending = {
            session
            for session, outcome in (await outcomes.outcomes()).items()
            if outcome == RESUMING
        }
        for session in conversation_marks.sessions():
            if session not in kept and session not in pending:
                conversation_marks.forget(session)

    async def key_claimed(key: str) -> bool:
        return await store.idempotency_key_claimed(key)  # type: ignore[attr-defined]

    async def send(session_id: SessionId, text: str) -> PromptDelivery:
        return await terminal.send_prompt(session_id, text)  # type: ignore[attr-defined]

    return RestartResume(
        snapshot=snapshot,
        enabled=enabled,
        resume=InterruptedResumePass(
            outcomes,
            resume=resume,
            folder_of=folder_of,
            bound_folder_of=bound_folder_of,
            key_claimed=key_claimed,
            send=send,
            stopping=stopping,
            now=lambda: datetime.now(UTC),
        ),
        forget_marks=forget_marks,
        notify=notify,
        exclusive=(
            (lambda: nullcontext(True)) if lock_path is None else partial(_held_alone, lock_path)
        ),
    )


@contextmanager
def _held_alone(path: Path) -> Iterator[bool]:
    """An exclusive, non-blocking `flock` on `path`: True while held here, False when another
    process holds it. Released when the descriptor closes, a crash included."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(descriptor)


async def _serve_with_reconciliation(
    secrets: TelegramSecrets,
    composition: ServiceComposition,
    serve_runner: Callable[[TelegramSecrets, PrivateBotBoundary], Awaitable[None]],
    interval: float,
    activity_interval: float = _ACTIVITY_POLL_SECONDS,
    trust_interval: float = _TRUST_POLL_SECONDS,
    limits_interval: float = _LIMITS_POLL_SECONDS,
    limit_stop_interval: float = _LIMIT_STOP_POLL_SECONDS,
    worktree_interval: float = _WORKTREE_POLL_SECONDS,
) -> None:
    """Poll Telegram while keeping durable records agreeing with observed panes.

    A launch that raises after its record is saved leaves that record STARTING, which no
    owner action can resolve, so reconciliation runs once before polling and periodically
    beside it. It never interrupts the service: a reconciliation that fails is logged and
    the pass is skipped, because a service that stops polling is worse than one whose
    records are briefly stale.

    RuntimeCoordinator composes the same three parts, but it treats polling returning as a
    failure; run_private_bot returns normally on SIGTERM, so adopting it would mean moving
    signal handling out of the polling boundary. That is a larger change to the shutdown
    path than this repair warrants.
    """
    # Rank before the first screen can be drawn. The composition hands the catalogue over in
    # registry order and the ranking is applied on refresh, so without this every start and
    # restart served an unranked Launch, Resume and search until the owner happened to press
    # Refresh — the common case, and the first thing an acceptance run looks at. It lives here
    # rather than inside the long-poll runner because `main` lets a test substitute the runner,
    # which makes this line reachable by a test; the runner is not.
    await composition.boundary.refresh_catalogue()
    # Before the first reconcile, which ends every record whose pane a lost server took with it
    # (DEC-127): after it, nothing says which of them were running.
    restart = await _restart_snapshot(composition)
    await _reconcile_quietly(composition)
    # Beside polling, not before it: the launches and the nudges' patience can take minutes,
    # and the bot must answer meanwhile -- and only once polling runs can a stop signal drain
    # the pass. The worktree cleanup alone waits for it (DEC-124): a resumed session must be
    # bound to its worktree before anything may judge that folder unreferenced.
    resuming = asyncio.create_task(_resume_after_restart(composition, restart))
    periodic = [resuming, asyncio.create_task(_reconcile_periodically(composition, interval))]
    if composition.restart_notifier is not None:
        periodic.append(asyncio.create_task(_flush_restart_notice(composition, resuming)))
    if composition.approval_watcher is not None or composition.activity_directory is not None:
        # A separate task rather than another step inside the reconciliation pass: the two
        # answer different questions on different clocks, and a pane-title read that hangs must
        # not stop records being reconciled. Nothing is polled before the service is serving,
        # unlike reconciliation -- a first pass at start-up could only establish a baseline
        # nothing may report from, since `CodexApprovalWatcher` treats a marker already present
        # on its first successful title read as a restart baseline rather than as news.
        #
        # Either source is reason enough to run it. A host serving only Claude sessions has no
        # pane anything can observe and a spool full of what those sessions reported, and gating
        # the whole pass on the watcher would have delivered none of it.
        periodic.append(
            asyncio.create_task(_watch_activity_periodically(composition, activity_interval))
        )
    if composition.prompt_relay is not None:
        # Its own clock, a fast one, for what a 30 s pass cannot see soon enough: a turn the
        # owner interrupted (no hook fires) and one whose "finished" landed before its screen
        # caught up. Idle unless a marked or just-finished session has a message waiting.
        periodic.append(
            asyncio.create_task(_check_waiting_turns_periodically(composition, _FAST_CHECK_SECONDS))
        )
    if composition.trust_notifier is not None:
        # Its own task on its own clock, for the reason the activity watch has one: a pass
        # that hangs must not stop records being reconciled, and these three answer different
        # questions. Unlike the activity watch it runs from the first tick -- there is no
        # baseline to establish, because an untrusted session standing at start-up is exactly
        # what the owner most needs to be told about.
        periodic.append(asyncio.create_task(_watch_trust_periodically(composition, trust_interval)))
    if composition.limit_reset_notifier is not None:
        # The fourth, on the same terms as the three above: its own task, its own clock, and it
        # never raises into serving. Unlike the trust watch it establishes a baseline on its
        # first tick and reports nothing from it — there is no "already true at start-up" here,
        # because a reset is a *change* and the first reading is only the thing it will be
        # compared against. Gated on the notifier rather than on `Backend.limits`: the notifier
        # is what holds the baseline and what was handed the read, so a wired one is the whole
        # condition and asking the backend again here would be a second opinion about it.
        periodic.append(
            asyncio.create_task(_watch_limits_periodically(composition, limits_interval))
        )
    if composition.schedule_pass is not None:
        # Its own task and clock, like the watchers: a pass that raises costs one tick.
        periodic.append(
            asyncio.create_task(_fire_schedules_periodically(composition, _SCHEDULE_POLL_SECONDS))
        )
    if composition.rollover_pass is not None:
        # Its own task and clock, like the schedule pass beside it: a pass that raises costs
        # one tick, and every step is taken again from what is persisted.
        periodic.append(
            asyncio.create_task(_roll_over_periodically(composition, _ROLLOVER_POLL_SECONDS))
        )
    if composition.limit_lift_watcher is not None:
        # Its own task and clock, on the same terms as the others: a pass that hangs or raises
        # costs this watch one tick and nothing else.
        periodic.append(
            asyncio.create_task(_watch_limit_stops_periodically(composition, limit_stop_interval))
        )
    if composition.limit_stop_outcomes is not None:
        periodic.append(asyncio.create_task(_prune_limit_stop_outcomes_periodically(composition)))
    if composition.worktree_cleanup is not None:
        # Its own task and clock, like the rollover pass: a pass that raises costs one tick.
        periodic.append(
            asyncio.create_task(
                _watch_worktrees_periodically(composition, worktree_interval, after=resuming)
            )
        )
    # Not another periodic task, and the difference is the point: the loops above each poll
    # something on a clock of their own, while this *subscribes* to a watcher that already
    # polls. Subscribing is what starts it (`StoreWatch.subscribe`), so there is no task to
    # create here and none to cancel -- detaching stops it.
    detach = _redraw_sessions_on_store_changes(composition)
    try:
        await serve_runner(secrets, composition.boundary)
    finally:
        if detach is not None:
            detach()
        try:
            # Drain first (BL-130). The passes enter the same gate, so cancelling them first
            # would cut a launch they started off part-way, and a drain after that would find
            # nothing left to wait for. A pass that wakes during the drain meets the shut gate
            # and leaves its schedule due, or its rollover where it stands, for the next start.
            await _drain_operations(composition)
        finally:
            # Still reached if `serve` is itself cancelled mid-drain: the passes and the two
            # helper children are reclaimed either way. Every other task first, with no turn of
            # the loop between the drain and their cancel, so none wakes on the shut gate.
            others = [task for task in periodic if task is not resuming]
            for task in others:
                task.cancel()
            await asyncio.gather(*others, return_exceptions=True)
            # Then the restart pass, which reads the shut gate and stops: a moment lets its
            # nudges record `stopping` rather than leave an intent read later as unconfirmed.
            if not resuming.done():
                await asyncio.wait({resuming}, timeout=_RESTART_STOP_GRACE_SECONDS)
            resuming.cancel()
            await asyncio.gather(resuming, return_exceptions=True)
            await _close_host_remote_control(composition)
            await _close_usage_readers(composition)


def _redraw_sessions_on_store_changes(composition: ServiceComposition):
    """Have the open sessions page follow the store, if this composition wired a watcher.

    The listener schedules and returns. `StateEvents.subscribe` is synchronous by contract and
    the watcher calls its listeners on its own polling task, so awaiting a Telegram edit inside
    one would stall the watcher for a network round trip -- on the task whose whole job is
    noticing things promptly.

    A host that wired no watcher gets exactly the behaviour it had before one existed: the
    page is drawn on a press. That is why the absence is answered with `None` rather than an
    error -- it is a composition that opted out, not one that is broken.
    """
    events = composition.boundary.backend.state_events
    if events is None:
        return None

    def redraw(change: StoreChanged) -> None:
        # Unread: `StoreChanged` carries a time and nothing else, because the watcher reads
        # file metadata and cannot say what moved. The redraw re-reads the page regardless.
        del change
        asyncio.create_task(_redraw_sessions(composition))

    return events.subscribe(redraw)


async def _redraw_sessions(composition: ServiceComposition) -> None:
    """One redraw attempt, with its failures swallowed the way the periodic passes swallow
    theirs: nobody asked for this edit, so a Telegram hiccup must not surface as an error the
    owner did not cause -- and must not take down the task that noticed the change."""
    try:
        drew = await composition.boundary.redraw_sessions_if_open()
        if not drew:
            # A change the edit floor suppressed is owed rather than dropped, and this is
            # where it gets paid: the watcher is already ticking, so letting it ask costs a
            # flag test and saves the boundary owning a timer of its own.
            await composition.boundary.settle_owed_redraw()
    except Exception:
        _LOG.warning("the sessions page could not be redrawn after a store change", exc_info=True)
    try:
        # The schedules list follows the store the same way, drawing only while on screen; a
        # failure here is its own, and costs the sessions page nothing.
        await composition.boundary.redraw_schedules_if_open()
    except Exception:
        _LOG.warning("the schedules list could not be redrawn after a store change", exc_info=True)


async def _drain_operations(composition: ServiceComposition) -> None:
    """Let operations already in flight finish, within `DRAIN_TIMEOUT_SECONDS`.

    A timeout is one warning and shutdown goes on: the gate stays shut, so nothing new starts,
    and an operation still running is cancelled with its pass a moment later -- exactly what
    every shutdown did before the drain existed.
    """
    if composition.drain is None:
        return
    try:
        await asyncio.wait_for(composition.drain(), timeout=DRAIN_TIMEOUT_SECONDS)
    except TimeoutError:
        _LOG.warning(
            "operations were still in flight after the %ss shutdown drain; stopping anyway",
            DRAIN_TIMEOUT_SECONDS,
        )


async def _close_host_remote_control(composition: ServiceComposition) -> None:
    """Reclaim the provider helper the host toggle may have started, and never raise here.

    Codex's Remote Control adapter keeps one `codex app-server proxy` child from the first
    status read onward. One per process, so this is orderly shutdown rather than a leak --
    but shutdown is exactly where an unreclaimed child becomes someone's stray process.

    Guarded on `is None` because a host whose providers declare no host-level toggle wires
    none (DEC-061), and swallowed because a failure to tidy up must not be the last thing a
    clean shutdown does.
    """
    backend = getattr(composition.boundary, "backend", None)
    if backend is None:
        return
    # Direct attribute access, not `getattr(..., None)`: `Backend` is a frozen slots
    # dataclass, so a renamed field is a loud AttributeError everywhere else in this
    # codebase. Swallowing it here would convert that into a silent `return` that leaves the
    # child unreclaimed with no signal at all -- the exact failure this function prevents.
    control = backend.host_remote_control
    if control is None:
        return
    try:
        # Bounded, because this runs in a `finally` on the way out. `aclose()` closes a
        # subprocess, and an unbounded await would make `systemctl stop` hang until
        # TimeoutStopSec, which then kills only the main process and orphans that very child.
        await asyncio.wait_for(control.aclose(), timeout=CLOSE_TIMEOUT_SECONDS)
    except TimeoutError:
        _LOG.warning("host remote control did not close within %ss", CLOSE_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 -- tidying up may not turn a clean stop into a crash
        _LOG.debug("host remote control did not close cleanly", exc_info=True)


async def _close_usage_readers(composition: ServiceComposition) -> None:
    """Reclaim the app-server child the account-limits read may have started; never raise.

    The same shape as `_close_host_remote_control`, for the same reason: Codex's account
    reader keeps one `codex app-server` child from the first limits read onward (sub-plan 01),
    and shutdown is where an unreclaimed child becomes a stray process. Bounded and swallowed
    on the same grounds, and reached through the declared field rather than a probe.
    """
    backend = getattr(composition.boundary, "backend", None)
    if backend is None:
        return
    close = backend.close_usage_readers
    if close is None:
        return
    try:
        await asyncio.wait_for(close(), timeout=CLOSE_TIMEOUT_SECONDS)
    except TimeoutError:
        _LOG.warning("usage readers did not close within %ss", CLOSE_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 -- tidying up may not turn a clean stop into a crash
        _LOG.debug("usage readers did not close cleanly", exc_info=True)


async def _watch_trust_periodically(composition: ServiceComposition, interval: float) -> None:
    """Ask, and amend, on a clock of its own — and never raise into the serving loop."""
    while True:
        await asyncio.sleep(interval)
        try:
            await composition.trust_notifier.pass_once()
        except Exception:
            # One pass, logged. A trust question that could not be asked this time is asked
            # next time; a loop that dies takes every later question with it.
            _LOG.exception("the folder-trust pass failed; it will be retried")


async def _watch_limits_periodically(composition: ServiceComposition, interval: float) -> None:
    """Ask each provider what its windows read, on a clock of its own — and never raise.

    The same shape as the trust watch, and the same contract: one pass, logged, and the loop
    goes on. A provider's reader that started failing must not stop reconciliation, activity or
    trust, because every one of those is more urgent than a limits figure being stale.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await composition.limit_reset_notifier.pass_once()
        except Exception:
            # One pass, logged. The baseline is the notifier's and survives this, so a tick lost
            # to a failing reader costs at most the detection that tick would have made.
            _LOG.exception("the limits watch could not complete a pass")


async def _watch_limit_stops_periodically(composition: ServiceComposition, interval: float) -> None:
    """Ask each undecided limit stop whether it lifted, on a clock of its own -- never raising."""
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.wait_for(
                composition.limit_lift_watcher.pass_once(),
                timeout=_LIMIT_STOP_PASS_TIMEOUT_SECONDS,
            )
        except Exception:
            # One pass, logged. Without a nudge the outcome is written only once a stop was acted
            # on, so a pass lost here is asked again thirty seconds later. A nudge runs shielded
            # from this bound and finishes -- record and line -- after a pass is cancelled.
            _LOG.exception("the limit-stop watch could not complete a pass")


async def _prune_limit_stop_outcomes_periodically(
    composition: ServiceComposition,
    interval: float = _PRUNE_POLL_SECONDS,
    first_delay: float = _PRUNE_FIRST_DELAY_SECONDS,
) -> None:
    """Prune finished limit-stop outcomes once a day -- never raising."""
    await asyncio.sleep(first_delay)
    while True:
        await _prune_limit_stop_outcomes_once(composition)
        await asyncio.sleep(interval)


async def _prune_limit_stop_outcomes_once(composition: ServiceComposition) -> None:
    """One prune, logged. A pass that raises costs a day, and nothing the rows guard is lost."""
    try:
        pruned = await prune_outcomes(composition.limit_stop_outcomes, now=datetime.now(UTC))
    except Exception:
        _LOG.exception("the limit-stop outcome prune could not complete")
        return
    if pruned:
        _LOG.info("pruned %d limit-stop outcome(s) older than the retention", pruned)


async def _fire_schedules_periodically(composition: ServiceComposition, interval: float) -> None:
    """Fire due schedules on a clock of their own, and resend a held notice -- never raising."""
    while True:
        await asyncio.sleep(interval)
        try:
            await composition.schedule_pass.run()
        except Exception:
            # One pass, logged. A schedule a pass could not reach stays due and is fired, or
            # reported missed, on a later one.
            _LOG.exception("the schedule pass failed; it will be retried")
        if composition.schedule_notifier is not None:
            try:
                await composition.schedule_notifier.pass_once()
            except Exception:
                _LOG.exception("a held schedule notice could not be resent")


async def _roll_over_periodically(composition: ServiceComposition, interval: float) -> None:
    """Walk envelopes and open rollovers on a clock of their own -- never raising."""
    while True:
        await asyncio.sleep(interval)
        try:
            await composition.rollover_pass.run_once()
        except Exception:
            # One pass, logged. Each state was persisted before its action, so the next pass
            # takes up exactly where this one stopped.
            _LOG.exception("the rollover pass failed; it will be retried")
        if composition.rollover_notifier is not None:
            try:
                await composition.rollover_notifier.pass_once()
            except Exception:
                _LOG.exception("a held rollover notice could not be resent")


async def _watch_worktrees_periodically(
    composition: ServiceComposition, interval: float, *, after: asyncio.Task | None = None
) -> None:
    """Remove the session worktrees every guard lets go, on a clock of their own -- never raising.

    Each removal runs inside the shared operation gate the pass was built with, so the drain at
    shutdown waits for one under way, and a pass that wakes after the gate shut removes nothing."""
    if after is not None:
        # Not before the restart resume pass has returned (DEC-124).
        await asyncio.wait({after})
    while True:
        await asyncio.sleep(interval)
        try:
            await composition.worktree_cleanup.pass_once()
        except Exception:
            # One pass, logged. Every guard is asked afresh on the next one.
            _LOG.exception("the worktree cleanup pass failed; it will be retried")


def build_worktree_cleanup(
    *,
    connection,
    git: GitCheckout,
    worktrees: WorktreeStore,
    sessions: SessionStore,
    project_paths: Mapping[ProjectId, Path],
    operation: Callable[[], AbstractAsyncContextManager[object]],
) -> WorktreeCleanupPass:
    """The worktree cleanup pass `serve` runs, and only `serve` (BL-116; built here, beside the
    loop that runs it, as the rollover pass is). It asks the same git boundary and rows the
    launches use (DEC-046), reads open rollovers from the domain store, and removes each folder
    inside `operation` -- the shared gate the drain shuts. `project_paths` is the live routing
    table, read per removal, so a project moved since its worktree was cut is asked afresh."""
    return WorktreeCleanupPass(
        git=git,
        store=worktrees,
        sessions=sessions,
        rollovers=SQLiteRolloverStore(connection),
        repository=partial(main_checkout, git, project_paths),
        operation=operation,
    )


def build_worktree_listing(
    *, connection, project_paths: Mapping[ProjectId, Path]
) -> Callable[[], Awaitable[tuple[tuple[str, str, str], ...]]]:
    """`doctor`'s read of every kept session worktree and why (BL-116): the cleanup pass's own
    guards, asked over a read-only connection, handed back as its `kept` listing alone -- so
    the one process that may remove folders is still `serve`."""
    git = GitCli()
    return WorktreeCleanupPass(
        git=git,
        store=SQLiteWorktreeStore(connection),
        sessions=SQLiteSessionStore(connection),
        rollovers=SQLiteRolloverStore(connection),
        repository=partial(main_checkout, git, project_paths),
    ).kept


def build_rollover_pass(
    *,
    connection,
    sessions: SessionService,
    terminal: TmuxTerminal,
    enabled: Callable[[], Awaitable[bool]],
    project_paths: Mapping[ProjectId, Path],
    adopt_templates: Mapping[ProfileId, str],
    notify: Callable[[RolloverReport], Awaitable[None]] | None = None,
    operation: Callable[[], AbstractAsyncContextManager[object]] = nullcontext,
    worktrees: WorktreeStore | None = None,
    reported: ReportedRoots | None = None,
    folders: FolderBindings | None = None,
) -> RolloverPass:
    """The rollover pass `serve` runs, and only `serve` (DEC-115, DEC-015/070: built here,
    beside the loop that runs it, so no other composition has one to run).

    Rows go in the domain store, so both surfaces redraw on a move (DEC-090). Launch and stop
    are the one session service's, so the pass takes the same locks every other caller does;
    typing is the terminal's guarded send (DEC-099). Envelopes are read under each project's
    git top level, where the planning plugin writes them; a project in no checkout has no root
    of its own, so its sessions roll over only from a root their writer reported.
    `project_paths` is the live routing table, read per pass.

    A session bound to a worktree reads its envelopes under that worktree's top level instead
    (BL-116): `worktrees` answers every binding in one read per pass. A worktree that is gone
    has no root of its own, so its sessions are not rolled over -- never read under the main
    checkout -- unless the session's writer reported a root.

    A session whose handoff writer reported another root reads its envelopes there (DEC-131):
    `reported` answers through `reported_roots_reader`. A session bound to a folder outside its
    project (`folders`, a successor started there) has that folder as its default root while it
    is still a git top level this user owns, and no root once it is not.
    """

    rollovers = SQLiteRolloverStore(connection)

    async def held_sessions() -> tuple[SessionId, ...]:
        named: list[SessionId] = []
        for rollover in await rollovers.open_rollovers():
            named.append(rollover.predecessor_session_id)
            if rollover.successor_session_id is not None:
                named.append(rollover.successor_session_id)
        return tuple(named)

    async def worktree_roots() -> dict[SessionId, Path | None]:
        by_path: dict[Path, Path | None] = {}
        roots: dict[SessionId, Path | None] = {}
        bound = {} if worktrees is None else await worktrees.bound_folders()
        for session_id, worktree in bound.items():
            if worktree.path not in by_path:
                folder = live_folder(worktree)
                # The worktree's own top level, which holds its `.git` file, or none: never a
                # walk up past it, which could reach any repository above the state folder.
                is_checkout = folder is not None and (folder / ".git").exists()
                by_path[worktree.path] = folder if is_checkout else None
            roots[session_id] = by_path[worktree.path]
        if folders is not None:
            for session_id, folder in (await folders.bound_folders()).items():
                roots.setdefault(session_id, acceptable_root(str(folder)))
        return roots

    return RolloverPass(
        rollovers,
        FileHandoffEnvelopes(),
        adopt_templates=adopt_templates,
        sessions=sessions.list_sessions,
        handoff_root=lambda project: handoff_root(project_paths.get(project)),
        worktree_roots=worktree_roots,
        reported_roots=(
            None
            if reported is None
            else reported_roots_reader(reported, sessions.list_sessions, held_sessions)
        ),
        enabled=enabled,
        idle=terminal.pane_idle,
        launch=sessions.launch,
        send=terminal.send_prompt,
        graceful_stop=sessions.graceful_stop,
        # Only failures are told (DEC-031); the notifier decides which, and words them.
        notify=notify,
        operation=operation,
    )


def reported_roots_reader(
    reported: ReportedRoots,
    sessions: Callable[[], Awaitable[Sequence[SessionRecord]]],
    held: Callable[[], Awaitable[Iterable[SessionId]]],
) -> Callable[[], Awaitable[dict[SessionId, Path]]]:
    """Each listed session's reported root, read once per pass (DEC-131).

    Only what the store's acceptance rule passes is answered, so a root that stopped being a
    git top level this user owns reads as no report and the session keeps its default root.
    A session no longer listed, or ENDED, has its report forgotten, as `forget_marks` forgets
    conversation marks: only ENDED, since an UNTRUSTED or STARTING session is live -- and
    never while an open rollover names it (`held`). A stopped predecessor is ENDED before its
    rollover finishes, and the successor's `accepted`, the envelopes' clean-up and the request's
    withdrawal are all read under its root until then. A report is a file, not a row, so this
    runs whether or not the rollover switch is on.
    """

    async def read() -> dict[SessionId, Path]:
        named = set(await held())
        kept = {
            str(record.session_id): record.session_id
            for record in await sessions()
            if record.state is not SessionState.ENDED or record.session_id in named
        }
        roots: dict[SessionId, Path] = {}
        for session in reported.sessions():
            if session not in kept:
                reported.forget(session)
                continue
            root = reported.latest(session)
            if root is not None:
                roots[kept[session]] = root
        return roots

    return read


def live_session_check(store: SessionStore) -> Callable[[str], Awaitable[bool]]:
    """`notifiable`'s answer for a session id, read from the store at the moment it is asked.

    The same predicate the bot drops a notification by, so the feed and the phone agree about
    which sessions can still have news. An id the store does not know is not live.
    """

    async def live(session_id: str) -> bool:
        try:
            key = SessionId.parse(session_id)
        except ValueError:
            return False
        record = await store.get(key)
        return record is not None and notifiable(record.state)

    return live


async def _watch_activity_periodically(composition: ServiceComposition, interval: float) -> None:
    # The roster first, not one interval later: the hooks trust whatever roster is on disk, and
    # one left by a long stop or a downgrade names sessions that ended and lacks any launched
    # since (DEC-126). Publishing is not polling, so the watcher's restart baseline is untouched.
    await _publish_hook_roster(composition)
    while True:
        await asyncio.sleep(interval)
        await _watch_activity_once(composition)


def _hold_paused_turns(
    composition: ServiceComposition,
    activities: Sequence[AgentActivity],
    now: float | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[AgentActivity]:
    """Hold back each paused "finished", and release it once its session has stayed quiet.

    A turn that ended with background work still in flight (DEC-129) usually wakes again when
    that work reports back, so its "finished" is held rather than delivered. The hold is dropped
    when the session does anything else -- any later activity, or a turn its hook marked after
    the pause -- and released as an ordinary `COMPLETED` after `_PAUSED_QUIET_SECONDS` of
    neither. Returns this pass's other activities, in order, with any released ones after them.

    **A released one is stamped with its release.** The feed lists rows in the order they were
    stored and ages each by its stamp, so one stored two minutes late under its pause's stamp
    sat above newer rows with an older age (seen live on 2026-10-17). The words are still the
    paused turn's own.
    """
    moment = time.monotonic() if now is None else now
    held = composition.paused_turns
    passing: list[AgentActivity] = []
    for activity in activities:
        if activity.paused:
            held[activity.session_id] = (activity, moment)
        else:
            held.pop(activity.session_id, None)
            passing.append(activity)
    markers = composition.turn_markers
    for session_id, (paused, since) in list(held.items()):
        started = markers.started_at(session_id) if markers is not None else None
        if started is not None and started > paused.observed_at:
            del held[session_id]
        elif moment - since >= _PAUSED_QUIET_SECONDS:
            del held[session_id]
            passing.append(replace(paused, paused=False, observed_at=clock()))
    return passing


async def _publish_hook_roster(composition: ServiceComposition) -> None:
    """Rewrite the hook rosters from the store -- guarded: a roster that cannot be written costs
    hooks, never the activity pass."""
    if composition.hook_roster is None:
        return
    try:
        await composition.hook_roster()
    except Exception:
        _LOG.exception("publishing the hook roster failed")


async def _watch_activity_once(composition: ServiceComposition) -> None:
    """One pass over both activity sources, delivered — and never raising.

    This loop runs beside the one that serves the owner, so a failure anywhere in it is logged
    and costs one pass. The two sources are gathered into one list on purpose: they answer the
    same question about different profiles, and an observation is owed the same weight
    regardless of which of them noticed.

    Gathering them is also what lets the notifier group by session across both. Codex is a
    hybrid source: a permission the provider reported in this same pass wins over the title
    edge, so the sources can meet in a pass without producing a fact and a guess about the same
    wait. One `deliver` call per source looks equivalent but quietly reintroduces two messages
    per session per pass.

    **Each source is guarded separately, and that is not tidiness.** `poll()` commits its own
    edge state as a side effect of deciding a marker just appeared -- it records the marker as
    seen before the activity reaches anyone, and re-arms only when the title clears again.
    Under one shared `try`, a drain that raised after a successful poll discarded that already
    committed observation, and that approval was then never reportable at all. The failure is
    invisible: nothing is lost that anything counts, and the owner simply never hears about an
    agent waiting on them.

    `deliver` is called even when both sources yielded nothing, because it also drains the
    retry queue an earlier pass may have left behind; returning early on an empty list would
    strand a backlog for as long as nothing new happened.

    The drain is a synchronous directory walk that unlinks what it reads, so it goes to a
    thread: this coroutine shares its event loop with Telegram long-polling and pane captures,
    and a spool with a backlog would otherwise stall both.
    """
    # Before the drain, so a session that ended since the last pass names nobody from here on.
    await _publish_hook_roster(composition)
    activities: list[AgentActivity] = []
    if composition.activity_directory is not None:
        try:
            activities.extend(
                await asyncio.to_thread(drain_activity, composition.activity_directory)
            )
        except Exception:
            _LOG.exception("draining the activity spool failed")
    try:
        activities = _hold_paused_turns(composition, activities)
    except Exception:
        _LOG.exception("holding paused turns failed; they are delivered as they arrived")
        activities = [replace(activity, paused=False) for activity in activities]
    if composition.approval_watcher is not None:
        try:
            reported_needs_answer_session_ids = tuple(
                dict.fromkeys(
                    activity.session_id
                    for activity in activities
                    if activity.confidence is ActivityConfidence.REPORTED
                    and activity.kind is ActivityKind.NEEDS_ANSWER
                )
            )
            composition.approval_watcher.mark_needs_answer_reported(
                reported_needs_answer_session_ids
            )
            activities.extend(await composition.approval_watcher.poll())
        except Exception:
            _LOG.exception("the Codex approval watch failed")
    if composition.limit_screen_watcher is not None:
        try:
            activities.extend(await composition.limit_screen_watcher.poll())
        except Exception:
            _LOG.exception("the limit-screen watch failed")
    if composition.limit_classifier is not None:
        try:
            activities = await asyncio.wait_for(
                composition.limit_classifier.classified(activities),
                timeout=_CLASSIFY_TIMEOUT_SECONDS,
            )
        except Exception:
            _LOG.exception("classifying limit stops failed; they are delivered as they arrived")
    if composition.activity_store is not None:
        for activity in activities:
            if composition.live_session is not None:
                try:
                    if not await composition.live_session(activity.session_id):
                        continue
                except Exception:
                    _LOG.exception("could not tell whether a session is live; recording anyway")
            try:
                await composition.activity_store.append(activity)
            except Exception:
                _LOG.exception("recording an activity observation failed; delivery continues")
    try:
        await composition.boundary.notifier.deliver(activities)
    except Exception:
        _LOG.exception("delivering activity notifications failed")
    await _retry_waiting_messages(composition, activities)


async def _retry_waiting_messages(
    composition: ServiceComposition, activities: list[AgentActivity]
) -> None:
    """Deliver each finished session's waiting message, then drop those of ended sessions.

    Only a COMPLETED activity retries -- the one "finished" event (Claude and Codex `Stop`,
    OpenCode `session.idle`, Cursor `stop`); a session waiting on an answer is exactly the one not
    to type into.
    The relay checks the pane again before typing (DEC-099), so a retry that finds it still busy
    leaves the message waiting for the next one. Guarded per session, like every step here: one
    failure costs that session one pass.
    """
    relay = composition.prompt_relay
    if relay is None:
        return
    if composition.relay_notice_flush is not None:
        try:
            await composition.relay_notice_flush()
        except Exception:
            _LOG.exception("sending held queued-message notices failed")
    finished = dict.fromkeys(
        activity.session_id for activity in activities if activity.kind is ActivityKind.COMPLETED
    )
    for session_id in finished:
        # Asked before the retry, so a retry that raises can be judged against it: most finished
        # sessions have nothing waiting, and a raise there is no drop.
        try:
            waited = relay.pending(SessionId.parse(session_id)) is not None
        except Exception:
            waited = False
        try:
            result = await relay.retry(SessionId.parse(session_id))
        except Exception:
            _LOG.exception("delivering a waiting message to %s failed", session_id)
            if waited:
                await _announce_if_dropped(composition, session_id)
            continue
        if result is not None and result.outcome is RelayOutcome.QUEUED:
            composition.relay_rechecks[session_id] = time.monotonic()
        if result is not None and composition.relay_announcer is not None:
            try:
                await composition.relay_announcer(session_id, result)
            except Exception:
                _LOG.exception("announcing a waiting message's delivery failed")
    try:
        await relay.sweep()
    except Exception:
        _LOG.exception("sweeping waiting messages failed")


async def _announce_if_dropped(composition: ServiceComposition, session_id: str) -> None:
    """After a retry that raised, tell the owner if their message is gone (BL-107).

    Called only for a session that had a message waiting before the retry. `PromptRelay.retry`
    settles a message it may have typed before re-raising, so the message is gone and nobody
    was told. A retry that raised before it claimed anything leaves the message waiting, and
    that is not news. So the queue is asked, not the exception: only a message no longer
    pending is reported, as one that may have been typed.
    """
    relay, announce = composition.prompt_relay, composition.relay_announcer
    if relay is None or announce is None:
        return
    try:
        if relay.pending(SessionId.parse(session_id)) is not None:
            return
        await announce(session_id, RelayResult(RelayOutcome.REFUSED))
    except Exception:
        _LOG.exception("reporting a dropped waiting message for %s failed", session_id)


async def _check_waiting_turns_periodically(
    composition: ServiceComposition, interval: float
) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await _check_waiting_turns_once(composition)
        except Exception:
            _LOG.exception("the waiting-message fast check failed")


async def _check_waiting_turns_once(
    composition: ServiceComposition, now: float | None = None
) -> None:
    """Retry the waiting message of each marked or just-finished session (BL-108, DEC-104).

    A marked session's turn may have ended with no hook: the retry's own capture, under the key
    lock, reads the marker and the screen, and types only on an idle composer with the turn
    shown over. A session with no marker is left for its "finished" event, because the screen
    alone reads Claude's streaming answer as idle. A just-finished one is retried until its
    screen catches up with its Stop. A result that is still QUEUED is said to nobody: the owner
    was told it was queued, and a tick that changes nothing must not message them again.
    """
    relay = composition.prompt_relay
    if relay is None:
        return
    moment = time.monotonic() if now is None else now
    rechecks = composition.relay_rechecks
    for session_id, since in list(rechecks.items()):
        if moment - since > _JUST_FINISHED_SECONDS:
            del rechecks[session_id]
    marked = composition.turn_markers.sessions() if composition.turn_markers is not None else ()
    # A snapshot of both, deliberately: the loop pops from `rechecks` as it goes.
    for session_id in dict.fromkeys((*marked, *rechecks)):
        try:
            session = SessionId.parse(session_id)
        except ValueError:
            continue
        try:
            if relay.pending(session) is None:
                rechecks.pop(session_id, None)
                continue
            result = await relay.retry(session)
        except Exception:
            _LOG.exception("re-checking a waiting message for %s failed", session_id)
            await _announce_if_dropped(composition, session_id)
            continue
        if result is None or result.outcome is RelayOutcome.QUEUED:
            continue
        rechecks.pop(session_id, None)
        if composition.relay_announcer is not None:
            try:
                await composition.relay_announcer(session_id, result)
            except Exception:
                _LOG.exception("announcing a waiting message's delivery failed")


async def _restart_snapshot(composition: ServiceComposition) -> RestartSnapshot | None:
    if composition.restart_resume is None:
        return None
    try:
        return await composition.restart_resume.snapshot()
    except Exception:
        _LOG.exception("the restart snapshot could not be taken")
        return None


async def _resume_after_restart(
    composition: ServiceComposition, snapshot: RestartSnapshot | None
) -> None:
    """Run the restart resume pass once, if the switch is on, and tell the owner -- never raising.

    With the switch off the snapshot is discarded and nothing is written."""
    restart = composition.restart_resume
    if restart is None or snapshot is None:
        return
    try:
        if not await restart.enabled():
            return
    except Exception:
        _LOG.exception("the restart resume switch could not be read; nothing is resumed")
        return
    try:
        holder = restart.exclusive()
        mine = holder.__enter__()
    except Exception:
        _LOG.exception("the restart resume lock could not be taken; nothing is resumed")
        return
    try:
        if not mine:
            _LOG.warning("another serve holds the restart resume pass; this one resumes nothing")
            return
        await _resume_held(restart, snapshot)
    finally:
        holder.__exit__(None, None, None)


async def _resume_held(restart: RestartResume, snapshot: RestartSnapshot) -> None:
    unchecked = 0
    reports: tuple[ResumeReport, ...] = ()
    if snapshot.server_known:
        try:
            reports = await restart.resume.run(snapshot)
        except Exception:
            # Every decision was written before the first launch; the next start resumes from
            # there -- and keeps the marks it will need, so the sweep is skipped.
            _LOG.exception("the restart resume pass failed")
            return
    else:
        unchecked = sum(
            1
            for session in snapshot.sessions
            if session.record.state is SessionState.RUNNING and not session.pane_listed
        )
    try:
        await restart.forget_marks()
    except Exception:
        _LOG.exception("the conversation marks could not be swept")
    if restart.notify is not None and (reports or unchecked):
        try:
            await restart.notify(reports, unchecked)
        except Exception:
            _LOG.exception("the restart notice could not be handed on")


async def _flush_restart_notice(composition: ServiceComposition, pass_: asyncio.Task) -> None:
    """Send the held restart notice once the bot is attached; done once the pass has returned
    and nothing is held."""
    notifier = composition.restart_notifier
    while not pass_.done() or notifier.pending:  # type: ignore[union-attr]
        await asyncio.sleep(_RESTART_NOTICE_POLL_SECONDS)
        try:
            await notifier.pass_once()  # type: ignore[union-attr]
        except Exception:
            _LOG.exception("the held restart notice could not be resent")


async def _reconcile_periodically(composition: ServiceComposition, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        await _reconcile_quietly(composition)


async def _reconcile_quietly(composition: ServiceComposition) -> None:
    try:
        await composition.reconciler.reconcile(await composition.terminal.managed_observations())
    except Exception:
        _LOG.exception("reconciliation pass failed")
