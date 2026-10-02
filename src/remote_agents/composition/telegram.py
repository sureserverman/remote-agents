"""Compose the Telegram service boundary over the shared backend."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path

from remote_agents.adapters.agents.registry import (
    profile_composers,
    profile_glyphs,
    profile_limit_screens,
    profile_trust_dialogs,
    profiles_keeping_a_draft,
    profiles_with_finished_events,
    provider_descriptors,
)
from remote_agents.adapters.agents.turn_markers import FileTurnMarkers
from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.sqlite.callback_state_store import SQLiteCallbackStateStore
from remote_agents.adapters.sqlite.chat_view_store import SQLiteChatViewStore
from remote_agents.adapters.sqlite.limit_stop_store import SQLiteLimitStopStore
from remote_agents.adapters.sqlite.queued_prompt_store import SQLiteQueuedPromptStore
from remote_agents.adapters.sqlite.schedule_store import SQLiteScheduleStore
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.standing_notification_store import (
    SQLiteStandingNotificationStore,
)
from remote_agents.adapters.sqlite.trust_notifications import SQLiteTrustNotificationStore
from remote_agents.adapters.telegram import FRONTEND
from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.application.activity import CodexApprovalWatcher
from remote_agents.application.limit_lifts import LimitLiftWatcher
from remote_agents.application.limit_resume import LimitResume
from remote_agents.application.limit_stops import LimitScreenWatcher, LimitStopClassifier
from remote_agents.application.prompt_relay import PromptRelay
from remote_agents.application.reconcile import ReconciliationService, SessionLocks
from remote_agents.application.schedule_times import host_zone
from remote_agents.application.schedules import SchedulePass, limit_stopped_in, still_working_in
from remote_agents.composition.backend import (
    ProjectCatalogueProvider,
    compose_backend,
    require_frontend_capabilities,
)
from remote_agents.composition.service import ServiceComposition
from remote_agents.composition.tui import _console_composer, _local_runtime
from remote_agents.config import (
    TelegramSecrets,
    read_claude_limits_source,
    read_cursor_limits_source,
    read_resume_after_limit,
)
from remote_agents.ports.agent_activity import ActivityKind
from remote_agents.ports.agent_usage import AgentLimits
from remote_agents.production import ProductionPaths


async def _no_limits() -> tuple[AgentLimits, ...]:
    """The limits read of a backend that has none: every stop lifts on its schedule alone."""
    return ()


def _private_boundary(
    config,
    connection,
    paths: ProductionPaths,
    secrets: TelegramSecrets,
    *,
    ui_connection,
) -> ServiceComposition:
    """Compose the Telegram surface over **two** stores.

    `connection` is the domain store — sessions, their events, their activity, and the
    idempotency claims that cross processes. `ui_connection` is what this surface writes about
    itself: callback tokens, the live view's anchor, standing and trust notifications.

    They are separate because `StoreWatch` fingerprints a file. While the four stores below held
    the domain connection, minting a keyboard changed the watched bytes, the watcher published a
    change, and the open sessions page redrew — republishing its own change about thirty times a
    minute until Telegram flood-banned the bot. Keyword-only and required, because a default
    here would silently restore exactly that.
    """
    projects = ProjectCatalogueProvider(config.registry_path, config.dev_root)
    # **One build of the descriptors for this whole composition**, with the owner's stated
    # ceiling, threaded into everything that folds them. Built here rather than left to each
    # caller because a descriptor constructs its vertical's collaborators — Claude's usage
    # reader among them — so three unthreaded folds meant three throwaway readers carrying this
    # project's assumed context window instead of the owner's number (DEC-061). `compose_backend`
    # makes the same build for the same reason; the gate evaluator found that fixing it there
    # left both production paths untouched, because each hands `compose_backend` a runtime it
    # had already built.
    descriptors = provider_descriptors(
        claude_context_window=(
            config.claude_context_window if config.claude_context_window_stated else None
        ),
        claude_context_window_stated=config.claude_context_window_stated,
        claude_limits_path=paths.claude_limits_path,
        # The file the config was loaded from, not `paths.config_path`: `--config` can name
        # another, and the switch must be read back from the one the owner is editing.
        claude_limits_switch=partial(read_claude_limits_source, config.path or paths.config_path),
        claude_home=paths.home,
        # Off unless the owner switched it on: the read uses the Cursor CLI's own login.
        cursor_limits_switch=partial(read_cursor_limits_source, config.path or paths.config_path),
        cursor_home=paths.home,
    )
    runtime = _local_runtime(config, paths, projects.paths, descriptors)
    terminal = runtime.terminal
    store = SQLiteSessionStore(connection)
    # One lock map, shared by the two objects that write session state. See the note on the
    # ReconciliationService below: this single binding is the fix, and two instances here
    # would look identical and repair nothing.
    locks = SessionLocks()
    # **The bot arranges the console too, for one operation only: stepping it aside before a
    # stop destroys a pane.** Without this the owner stopping a displayed session from their
    # phone left the agent's pane to be killed *inside* the console window, so the console sat
    # a pane short — sessions and feed stretched across the whole width — until its next
    # reload put the projects surface back, up to ten seconds later.
    #
    # This is the half of DEC-005 that is answered rather than accepted. Its premise was one
    # writer over the panes by construction, and what made a second one safe is
    # `console_lock`: both composers are built by `_console_composer`, so both name the same
    # lock file, and neither decides from a reading the other is about to invalidate. The bot
    # never *builds* a console — nothing here calls `ensure` — and `hide` degrades to nothing
    # on a host with no console at all, which is every host that has not run `remote-agents`.
    console = _console_composer(runtime.gateway, paths.home)
    # The one backend this process hands its frontend (ARCH-B1). `locks` and the console
    # hide are the service's own wiring and go in here; the reconciler and approval watcher
    # below are not the frontend's to drive and stay outside it (ARCH-B3).
    backend = require_frontend_capabilities(
        FRONTEND,
        compose_backend(
            config,
            connection,
            paths,
            projects=projects,
            runtime=runtime,
            # The same store the reconciler and approval watcher below are given. Inert
            # today -- SQLiteSessionStore holds only its connection -- but two instances
            # where there was one stops being inert the moment it gains a cache or a
            # statement pool, and this composition is the one place all three consumers are
            # meant to agree.
            store=store,
            locks=locks,
            hide_in_console=console.hide,
        ),
    )
    # Named rather than inlined, because the serve loop needs the pass the factory built: the
    # boundary owns it (it speaks through the live view and mints against the same callbacks),
    # and `ServiceComposition` is what puts it on a clock.
    finishing = profiles_with_finished_events(descriptors)
    # The markers the agents' hooks write into the spool (BL-108, DEC-104): the relay's sweep
    # ends those of sessions that are gone, and the service's fast check reads which to re-check.
    turn_markers = FileTurnMarkers(paths.activity_directory)
    relay = PromptRelay(
        terminal,
        SQLiteQueuedPromptStore(ui_connection),
        store,
        queues_for=lambda profile: str(profile) in finishing,
        turn_markers=turn_markers,
    )
    boundary = build_private_bot(
        secrets.owner_user_id,
        secrets.owner_chat_id,
        # The durable store, not the in-memory default: a restart used to void every
        # button in the chat, and only this half of the pair actually fixes that.
        callbacks=SQLiteCallbackStateStore(ui_connection),
        # And the durable anchor for the same reason: a restart that forgot which
        # message the live view is would send a second one and leave the first above it,
        # still holding buttons that — since Stage 1 — still resolve.
        anchors=SQLiteChatViewStore(ui_connection),
        # And the durable standing notifications, which close the other half of that
        # same defect. A restart that forgot which message a session's notification is
        # sent a *second* one on the session's next report and left the first above the
        # live view — observed in the chat on 2026-08-20, when the 21:23 restart turned
        # one session's alert into one message above the menu and one below.
        standing=SQLiteStandingNotificationStore(ui_connection),
        # The whole backend, not five of its fields taken out and handed over one at a
        # time. `catalogue` and `max_label_length` came through here too and are on it;
        # the boundary seeds its render copy of the first from `Backend.catalogue`.
        backend=backend,
        # Profiles come off the backend like everything else now. They were a separate
        # argument for as long as `Backend.profiles` held the domain type and this
        # surface needed its own narrowing; `compose_backend` does that narrowing once,
        # so the line that used to be the plausible-looking mistake is the correct one.
        profiles=backend.profiles,
        # Each curated profile's mark, folded here rather than looked up at render time.
        # The registry is the one module that may import every vertical (ARCH-02/ARCH-04),
        # and the bot is one that may import none, so the mapping is built on this side of
        # that line and handed over — the same shape `usage_readers` is folded in with.
        glyphs=profile_glyphs(descriptors),
        # The other provider fact this surface is handed rather than knowing: which
        # profiles it may offer *both* answers to. Read off the same registry as the
        # marks, and the same mapping the terminal is given, so the button and the
        # keypress cannot disagree about who can be answered.
        trust_dialogs=profile_trust_dialogs(descriptors),
        project_page_size=config.project_page_size,
        # The durable home for the one standing trust question per session (migration 12).
        # Its absence is what a boundary without a trust pass looks like, so supplying it is
        # the whole of the wiring here.
        trust_store=SQLiteTrustNotificationStore(ui_connection),
        # The prompt relay (DEC-099): its queue lives in the UI store (DEC-090), it types only
        # through the terminal both surfaces share, and it queues only for agents whose
        # "finished" event this service drains -- all read off the one descriptor set.
        message_relay=relay,
        relayable=frozenset(profile_composers(descriptors)),
    )
    limit_stops = SQLiteLimitStopStore(connection)
    # The fire pass (DEC-114): launches through the one session use case, types through the
    # terminal's guarded send, and tells the owner through the boundary's notifier. Wired only
    # where the backend manages schedules, which `compose_backend` always does in production.
    schedule_pass = (
        None
        if backend.schedules is None
        or backend.sessions is None
        or boundary.schedule_notifier is None
        else SchedulePass(
            SQLiteScheduleStore(connection),
            launch=backend.sessions.launch,
            send=terminal.send_prompt,
            limit_stopped=limit_stopped_in(store, limit_stops),
            working=still_working_in(store, turn_markers),
            notify=boundary.schedule_notifier.notify,
            zone=host_zone,
        )
    )
    return ServiceComposition(
        boundary,
        terminal,
        # Readiness is wired in deliberately: without it, reconciliation promotes any
        # FAILED session with a live pane to RUNNING, including one stopped dead on a
        # trust dialog it cannot answer. Observed in the wild 2026-08-14.
        #
        # The locks are shared with the SessionService above, and that sharing is the whole
        # fix for the InvalidTransition crashes: the reconciler runs on a timer beside the
        # service and writes `record_event` directly, so without a lock in common it would
        # overwrite the state of a session whose graceful stop is between its own two writes.
        # Constructing two SessionLocks here would type-check, run, and fix nothing.
        ReconciliationService(store, confirm_ready=terminal.confirm_ready, locks=locks),
        CodexApprovalWatcher(store, terminal.pane_title),
        paths.activity_directory,
        SQLiteActivityStore(connection),
        trust_notifier=boundary.trust_notifier,
        limit_reset_notifier=boundary.limit_reset_notifier,
        prompt_relay=relay,
        relay_announcer=boundary.announce_relayed,
        turn_markers=turn_markers,
        # The limits read both surfaces share, and each vertical's own limit sentence: a limit
        # stop is given the window that stopped it before it is recorded or delivered.
        limit_classifier=(
            None
            if backend.limits is None
            else LimitStopClassifier(store, backend.limits, profile_limit_screens(descriptors))
        ),
        # Codex and Cursor Agent report no limit event, so their stop is read off the pane.
        limit_screen_watcher=LimitScreenWatcher(
            store, terminal.capture, profile_limit_screens(descriptors)
        ),
        # A stop whose limit lifted has its line retired from the bot's message, once. The same
        # shared limits read as the classifier; without one, stops lift on their schedule only.
        limit_lift_watcher=LimitLiftWatcher(
            store,
            limit_stops,
            backend.limits if backend.limits is not None else _no_limits,
            lambda stop: boundary.notifier.retire_line(
                stop.session_id, ActivityKind.LIMIT_REACHED, observed_at=stop.stopped_at
            ),
            # And, with the Settings switch on (its default), one nudge typed into the lifted
            # session through the terminal's guarded send -- never the owner's relay queue.
            resume=LimitResume(
                send=terminal.send_prompt,
                enabled=_resume_switch(
                    backend.resume_after_limit, config.path or paths.config_path
                ),
                settled=lambda stop: boundary.notifier.line_settled(
                    stop.session_id, ActivityKind.LIMIT_REACHED, observed_at=stop.stopped_at
                ),
                amend=lambda stop, reason: boundary.notifier.retire_line(
                    stop.session_id,
                    ActivityKind.LIMIT_REACHED,
                    observed_at=stop.stopped_at,
                    note=reason,
                ),
            ),
            # Cursor keeps the owner's draft at its stop, so its lift is retired, never nudged.
            retire_only=profiles_keeping_a_draft(descriptors),
        ),
        schedule_pass=schedule_pass,
        schedule_notifier=boundary.schedule_notifier,
    )


def _resume_switch(setting: object | None, path: Path) -> Callable[[], Awaitable[bool]]:
    """The service's read of the resume switch: fresh, and off on any doubt.

    Not the Settings row's read. That one draws the default for a file it cannot read; this one
    decides whether the service types into a pane, so a half-edited file reads as off
    (`read_resume_after_limit(when_unsure=False)`). A composition that wired no switch is off.
    """
    if setting is None:

        async def off() -> bool:
            return False

        return off

    async def read() -> bool:
        return await asyncio.to_thread(read_resume_after_limit, path, when_unsure=False)

    return read
