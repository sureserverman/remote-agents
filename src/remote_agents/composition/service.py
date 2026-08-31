"""Compose the serving loop: the bot boundary plus its record-keeping companions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.adapters.tmux.runtime import TmuxTerminal
from remote_agents.application.activity import CodexApprovalWatcher, drain_activity
from remote_agents.application.reconcile import ReconciliationService
from remote_agents.config import TelegramSecrets
from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind, AgentActivity

_LOG = logging.getLogger(__name__)
_ACTIVITY_POLL_SECONDS = 30.0


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


async def _serve_with_reconciliation(
    secrets: TelegramSecrets,
    composition: ServiceComposition,
    serve_runner: Callable[[TelegramSecrets, PrivateBotBoundary], Awaitable[None]],
    interval: float,
    activity_interval: float = _ACTIVITY_POLL_SECONDS,
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
    await _reconcile_quietly(composition)
    periodic = [asyncio.create_task(_reconcile_periodically(composition, interval))]
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
    try:
        await serve_runner(secrets, composition.boundary)
    finally:
        for task in periodic:
            task.cancel()
        await asyncio.gather(*periodic, return_exceptions=True)


async def _watch_activity_periodically(composition: ServiceComposition, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        await _watch_activity_once(composition)


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
    activities: list[AgentActivity] = []
    if composition.activity_directory is not None:
        try:
            activities.extend(
                await asyncio.to_thread(drain_activity, composition.activity_directory)
            )
        except Exception:
            _LOG.exception("draining the activity spool failed")
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
    if composition.activity_store is not None:
        for activity in activities:
            try:
                await composition.activity_store.append(activity)
            except Exception:
                _LOG.exception("recording an activity observation failed; delivery continues")
    try:
        await composition.boundary.notifier.deliver(activities)
    except Exception:
        _LOG.exception("delivering activity notifications failed")


async def _reconcile_periodically(composition: ServiceComposition, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        await _reconcile_quietly(composition)


async def _reconcile_quietly(composition: ServiceComposition) -> None:
    try:
        await composition.reconciler.reconcile(await composition.terminal.managed_observations())
    except Exception:
        _LOG.exception("reconciliation pass failed")
