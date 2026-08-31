"""Compose the Telegram service boundary over the shared backend."""

from __future__ import annotations

from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.sqlite.callback_state_store import SQLiteCallbackStateStore
from remote_agents.adapters.sqlite.chat_view_store import SQLiteChatViewStore
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.standing_notification_store import (
    SQLiteStandingNotificationStore,
)
from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.application.activity import CodexApprovalWatcher
from remote_agents.application.reconcile import ReconciliationService, SessionLocks
from remote_agents.composition.backend import ProjectCatalogueProvider, compose_backend
from remote_agents.composition.service import ServiceComposition
from remote_agents.config import TelegramSecrets
from remote_agents.production import ProductionPaths


def _private_boundary(
    config, connection, paths: ProductionPaths, secrets: TelegramSecrets
) -> ServiceComposition:
    # Deferred: `_local_runtime` and `_console_composer` still live in `bootstrap` until the
    # tui extraction, and module-scope imports of them would cycle (bootstrap imports this
    # module).
    from remote_agents.bootstrap import _console_composer, _local_runtime

    projects = ProjectCatalogueProvider(config.registry_path, config.dev_root)
    runtime = _local_runtime(config, paths, projects.paths)
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
    backend = compose_backend(
        config,
        connection,
        paths,
        projects=projects,
        runtime=runtime,
        # The same store the reconciler and approval watcher below are given. Inert today --
        # SQLiteSessionStore holds only its connection -- but two instances where there was
        # one stops being inert the moment it gains a cache or a statement pool, and this
        # composition is the one place all three consumers are meant to agree.
        store=store,
        locks=locks,
        hide_in_console=console.hide,
    )
    return ServiceComposition(
        # The factory, not the class: it wires the stop controller, the live view and the
        # notifier, which the boundary used to build for itself out of whatever it had.
        build_private_bot(
            secrets.owner_user_id,
            secrets.owner_chat_id,
            # The durable store, not the in-memory default: a restart used to void every
            # button in the chat, and only this half of the pair actually fixes that.
            callbacks=SQLiteCallbackStateStore(connection),
            # And the durable anchor for the same reason: a restart that forgot which
            # message the live view is would send a second one and leave the first above it,
            # still holding buttons that — since Stage 1 — still resolve.
            anchors=SQLiteChatViewStore(connection),
            # And the durable standing notifications, which close the other half of that
            # same defect. A restart that forgot which message a session's notification is
            # sent a *second* one on the session's next report and left the first above the
            # live view — observed in the chat on 2026-08-20, when the 21:23 restart turned
            # one session's alert into one message above the menu and one below.
            standing=SQLiteStandingNotificationStore(connection),
            # The whole backend, not five of its fields taken out and handed over one at a
            # time. `catalogue` and `max_label_length` came through here too and are on it;
            # the boundary seeds its render copy of the first from `Backend.catalogue`.
            backend=backend,
            # Profiles come off the backend like everything else now. They were a separate
            # argument for as long as `Backend.profiles` held the domain type and this
            # surface needed its own narrowing; `compose_backend` does that narrowing once,
            # so the line that used to be the plausible-looking mistake is the correct one.
            profiles=backend.profiles,
            project_page_size=config.project_page_size,
        ),
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
    )
